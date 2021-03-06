## This file is part of Invenio.
## Copyright (C) 2008, 2009, 2010, 2011, 2012, 2013, 2020 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""
Invenio utilities to run SQL queries.

The main API functions are:
    - run_sql()
    - run_sql_many()
    - run_sql_with_limit()
but see the others as well.
"""

__revision__ = "$Id$"

# dbquery clients can import these from here:
# pylint: disable=W0611
from MySQLdb import Warning, Error, InterfaceError, DataError, \
                    DatabaseError, OperationalError, IntegrityError, \
                    InternalError, NotSupportedError, \
                    ProgrammingError
import gc
import os
import string
import time
import marshal
import re
import atexit

from zlib import compress, decompress
from thread import get_ident
from invenio.config import CFG_ACCESS_CONTROL_LEVEL_SITE, \
    CFG_MISCUTIL_SQL_USE_SQLALCHEMY, \
    CFG_MISCUTIL_SQL_RUN_SQL_MANY_LIMIT
from invenio.gc_workaround import gcfix

if CFG_MISCUTIL_SQL_USE_SQLALCHEMY:
    try:
        import sqlalchemy.pool as pool
        import MySQLdb as mysqldb
        mysqldb = pool.manage(mysqldb, use_threadlocal=True)
        connect = mysqldb.connect
    except ImportError:
        CFG_MISCUTIL_SQL_USE_SQLALCHEMY = False
        from MySQLdb import connect
else:
    from MySQLdb import connect

## DB config variables.  These variables are to be set in
## invenio-local.conf by admins and then replaced in situ in this file
## by calling "inveniocfg --update-dbexec".
## Note that they are defined here and not in config.py in order to
## prevent them from being exported accidentally elsewhere, as no-one
## should know DB credentials but this file.
## FIXME: this is more of a blast-from-the-past that should be fixed
## both here and in inveniocfg when the time permits.
try:
    from invenio.dbquery_config import (
        CFG_DATABASE_HOST,
        CFG_DATABASE_PORT,
        CFG_DATABASE_NAME,
        CFG_DATABASE_USER,
        CFG_DATABASE_PASS)
except ImportError:
    CFG_DATABASE_HOST = 'localhost'
    CFG_DATABASE_PORT = '3306'
    CFG_DATABASE_NAME = 'invenio'
    CFG_DATABASE_USER = 'invenio'
    CFG_DATABASE_PASS = 'my123p$ss'

try:
    from invenio.dbquery_config import (
        CFG_DATABASE_SLAVE,
        CFG_DATABASE_SLAVE_PORT,
        CFG_DATABASE_SLAVE_SU_USER,
        CFG_DATABASE_SLAVE_SU_PASS,
        CFG_DATABASE_PASSWORD_FILE
    )
except ImportError:
    CFG_DATABASE_SLAVE = ''
    CFG_DATABASE_SLAVE_PORT = ''
    CFG_DATABASE_SLAVE_SU_USER = ''
    CFG_DATABASE_SLAVE_SU_PASS = ''
    CFG_DATABASE_PASSWORD_FILE = ''

def _get_password_from_database_password_file(user):
    """
    Parse CFG_DATABASE_PASSWORD_FILE and return password
    corresponding to user.
    """
    if os.path.exists(CFG_DATABASE_PASSWORD_FILE):
        for row in open(CFG_DATABASE_PASSWORD_FILE):
            if row.strip():
                a_user, pwd = row.strip().split(" // ")
                if user == a_user:
                    return pwd
        raise ValueError("user '%s' not found in database password file '%s'" % (user, CFG_DATABASE_PASSWORD_FILE))
    raise IOError("No password defined for user '%s' but database password file is not available" % user)

if CFG_DATABASE_SLAVE_SU_USER and not CFG_DATABASE_SLAVE_SU_PASS and CFG_DATABASE_PASSWORD_FILE:
    CFG_DATABASE_SLAVE_SU_PASS = _get_password_from_database_password_file(CFG_DATABASE_SLAVE_SU_USER)

_DB_CONN = {}
_DB_CONN[CFG_DATABASE_HOST] = {}
_DB_CONN[CFG_DATABASE_SLAVE] = {}

def get_connection_for_dump_on_slave():
    """
    Return a valid connection, suitable to perform dump operation
    on a slave node of choice.
    """
    dbport = int(CFG_DATABASE_PORT)
    if CFG_DATABASE_SLAVE_PORT:
        dbport = int(CFG_DATABASE_SLAVE_PORT)
    connection = connect(host=CFG_DATABASE_SLAVE,
                         port=dbport,
                         db=CFG_DATABASE_NAME,
                         user=CFG_DATABASE_SLAVE_SU_USER,
                         passwd=CFG_DATABASE_SLAVE_SU_PASS,
                         use_unicode=False, charset='utf8')
    connection.autocommit(True)
    return connection


def unlock_all():
    for dbhost in _DB_CONN.keys():
        for db in _DB_CONN[dbhost].values():
            try:
                cur = db.cur()
                cur.execute("UNLOCK TABLES")
            except:
                pass

atexit.register(unlock_all)

class InvenioDbQueryWildcardLimitError(Exception):
    """Exception raised when query limit reached."""
    def __init__(self, res):
        """Initialization."""
        self.res = res

def _db_login(dbhost=CFG_DATABASE_HOST, relogin=0):
    """Login to the database."""

    ## Note: we are using "use_unicode=False", because we want to
    ## receive strings from MySQL as Python UTF-8 binary string
    ## objects, not as Python Unicode string objects, as of yet.

    ## Note: "charset='utf8'" is needed for recent MySQLdb versions
    ## (such as 1.2.1_p2 and above).  For older MySQLdb versions such
    ## as 1.2.0, an explicit "init_command='SET NAMES utf8'" parameter
    ## would constitute an equivalent.  But we are not bothering with
    ## older MySQLdb versions here, since we are recommending to
    ## upgrade to more recent versions anyway.

    dbport = int(CFG_DATABASE_PORT)
    if dbhost == CFG_DATABASE_SLAVE and CFG_DATABASE_SLAVE_PORT:
        dbport = int(CFG_DATABASE_SLAVE_PORT)

    if CFG_MISCUTIL_SQL_USE_SQLALCHEMY:
        return connect(host=dbhost,
                       port=dbport,
                       db=CFG_DATABASE_NAME,
                       user=CFG_DATABASE_USER,
                       passwd=CFG_DATABASE_PASS,
                       use_unicode=False, charset='utf8')
    else:
        thread_ident = (os.getpid(), get_ident())
    if relogin:
        connection = _DB_CONN[dbhost][thread_ident] = connect(host=dbhost,
                                                              port=dbport,
                                                              db=CFG_DATABASE_NAME,
                                                              user=CFG_DATABASE_USER,
                                                              passwd=CFG_DATABASE_PASS,
                                                              use_unicode=False, charset='utf8')
        connection.autocommit(True)
        return connection
    else:
        if _DB_CONN[dbhost].has_key(thread_ident):
            return _DB_CONN[dbhost][thread_ident]
        else:
            connection = _DB_CONN[dbhost][thread_ident] = connect(host=dbhost,
                                                                  port=dbport,
                                                                  db=CFG_DATABASE_NAME,
                                                                  user=CFG_DATABASE_USER,
                                                                  passwd=CFG_DATABASE_PASS,
                                                                  use_unicode=False, charset='utf8')
            connection.autocommit(True)
            return connection

def _db_logout(dbhost=CFG_DATABASE_HOST):
    """Close a connection."""
    try:
        del _DB_CONN[dbhost][(os.getpid(), get_ident())]
    except KeyError:
        pass

def close_connection(dbhost=CFG_DATABASE_HOST):
    """
    Enforce the closing of a connection
    Highly relevant in multi-processing and multi-threaded modules
    """
    try:
        db = _DB_CONN[dbhost][(os.getpid(), get_ident())]
        cur = db.cursor()
        cur.execute("UNLOCK TABLES")
        db.close()
        del _DB_CONN[dbhost][(os.getpid(), get_ident())]
    except KeyError:
        pass

def run_sql(sql, param=None, n=0, with_desc=False, with_dict=False, run_on_slave=False, connection=None):
    """Run SQL on the server with PARAM and return result.
    @param param: tuple of string params to insert in the query (see
    notes below)
    @param n: number of tuples in result (0 for unbounded)
    @param with_desc: if True, will return a DB API 7-tuple describing
    columns in query.
    @param with_dict: if True, will return a list of dictionaries
    composed of column-value pairs
    @param connection: if provided, uses the given connection.
    @return: If SELECT, SHOW, DESCRIBE statements, return tuples of data,
    followed by description if parameter with_desc is
    provided.
    If SELECT and with_dict=True, return a list of dictionaries
    composed of column-value pairs, followed by description
    if parameter with_desc is provided.
    If INSERT, return last row id.
    Otherwise return SQL result as provided by database.

    @note: When the site is closed for maintenance (as governed by the
    config variable CFG_ACCESS_CONTROL_LEVEL_SITE), do not attempt
    to run any SQL queries but return empty list immediately.
    Useful to be able to have the website up while MySQL database
    is down for maintenance, hot copies, table repairs, etc.
    @note: In case of problems, exceptions are returned according to
    the Python DB API 2.0.  The client code can import them from
    this file and catch them.
    """

    if CFG_ACCESS_CONTROL_LEVEL_SITE == 3:
        # do not connect to the database as the site is closed for maintenance:
        return []
    elif CFG_ACCESS_CONTROL_LEVEL_SITE > 0:
        ## Read only website
        if not sql.upper().startswith("SELECT") and not sql.upper().startswith("SHOW"):
            return

    if param:
        param = tuple(param)

    dbhost = CFG_DATABASE_HOST
    if run_on_slave and CFG_DATABASE_SLAVE:
        dbhost = CFG_DATABASE_SLAVE

    ### log_sql_query(dbhost, sql, param) ### UNCOMMENT ONLY IF you REALLY want to log all queries
    try:
        db = connection or _db_login(dbhost)
        cur = db.cursor()
        gc.disable()
        rc = cur.execute(sql, param)
        gc.enable()
    except (OperationalError, InterfaceError): # unexpected disconnect, bad malloc error, etc
        # FIXME: now reconnect is always forced, we may perhaps want to ping() first?
        if connection is not None:
            raise
        try:
            time.sleep(30) # In case of DB restart give it 30s to breath.
            db = _db_login(dbhost, relogin=1)
            cur = db.cursor()
            gc.disable()
            rc = cur.execute(sql, param)
            gc.enable()
        except (OperationalError, InterfaceError): # unexpected disconnect, bad malloc error, etc
            raise

    if string.upper(string.split(sql)[0]) in ("SELECT", "SHOW", "DESC", "DESCRIBE"):
        if n:
            recset = cur.fetchmany(n)
        else:
            recset = cur.fetchall()

        if with_dict: # return list of dictionaries
            # let's extract column names
            keys = [row[0] for row in cur.description]
            # let's construct a list of dictionaries
            list_dict_results = [dict(zip(*[keys, values])) for values in recset]

            if with_desc:
                return list_dict_results, cur.description
            else:
                return list_dict_results
        else:
            if with_desc:
                return recset, cur.description
            else:
                return recset
    else:
        if string.upper(string.split(sql)[0]) == "INSERT":
            rc = cur.lastrowid
        return rc

def run_sql_many(query, params, limit=CFG_MISCUTIL_SQL_RUN_SQL_MANY_LIMIT, run_on_slave=False):
    """Run SQL on the server with PARAM.
    This method does executemany and is therefore more efficient than execute
    but it has sense only with queries that affect state of a database
    (INSERT, UPDATE). That is why the results just count number of affected rows

    @param params: tuple of tuple of string params to insert in the query

    @param limit: query will be executed in parts when number of
         parameters is greater than limit (each iteration runs at most
         `limit' parameters)

    @return: SQL result as provided by database
    """
    if CFG_ACCESS_CONTROL_LEVEL_SITE == 3:
        # do not connect to the database as the site is closed for maintenance:
        return []
    elif CFG_ACCESS_CONTROL_LEVEL_SITE > 0:
        ## Read only website
        if not query.upper().startswith("SELECT") and not query.upper().startswith("SHOW"):
            return

    dbhost = CFG_DATABASE_HOST
    if run_on_slave and CFG_DATABASE_SLAVE:
        dbhost = CFG_DATABASE_SLAVE
    i = 0
    r = None
    while i < len(params):
        ## make partial query safely (mimicking procedure from run_sql())
        try:
            db = _db_login(dbhost)
            cur = db.cursor()
            gc.disable()
            rc = cur.executemany(query, params[i:i + limit])
            gc.enable()
        except (OperationalError, InterfaceError):
            try:
                db = _db_login(dbhost, relogin=1)
                cur = db.cursor()
                gc.disable()
                rc = cur.executemany(query, params[i:i + limit])
                gc.enable()
            except (OperationalError, InterfaceError):
                raise
        ## collect its result:
        if r is None:
            r = rc
        else:
            r += rc
        i += limit
    return r

def run_sql_with_limit(query, param=None, n=0, with_desc=False, wildcard_limit=0, run_on_slave=False):
    """This function should be used in some cases, instead of run_sql function, in order
        to protect the db from queries that might take a log time to respond
        Ex: search queries like [a-z]+ ; cern*; a->z;
        The parameters are exactly the ones for run_sql function.
        In case the query limit is reached, an InvenioDbQueryWildcardLimitError will be raised.
    """
    try:
        dummy = int(wildcard_limit)
    except ValueError:
        raise
    if wildcard_limit < 1:#no limit on the wildcard queries
        return run_sql(query, param, n, with_desc, run_on_slave=run_on_slave)
    safe_query = query + " limit %s" %wildcard_limit
    res = run_sql(safe_query, param, n, with_desc, run_on_slave=run_on_slave)
    if len(res) == wildcard_limit:
        raise InvenioDbQueryWildcardLimitError(res)
    return res

def blob_to_string(ablob):
    """Return string representation of ABLOB.  Useful to treat MySQL
    BLOBs in the same way for both recent and old MySQLdb versions.
    """
    if ablob:
        if type(ablob) is str:
            # BLOB is already a string in MySQLdb 0.9.2
            return ablob
        else:
            # BLOB is array.array in MySQLdb 1.0.0 and later
            return ablob.tostring()
    else:
        return ablob

def log_sql_query(dbhost, sql, param=None):
    """Log SQL query into prefix/var/log/dbquery.log log file.  In order
       to enable logging of all SQL queries, please uncomment one line
       in run_sql() above. Useful for fine-level debugging only!
    """
    from invenio.config import CFG_LOGDIR
    from invenio.dateutils import convert_datestruct_to_datetext
    from invenio.textutils import indent_text
    log_path = CFG_LOGDIR + '/dbquery.log'
    date_of_log = convert_datestruct_to_datetext(time.localtime())
    message = date_of_log + '-->\n'
    message += indent_text('Host:\n' + indent_text(str(dbhost), 2, wrap=True), 2)
    message += indent_text('Query:\n' + indent_text(str(sql), 2, wrap=True), 2)
    message += indent_text('Params:\n' + indent_text(str(param), 2, wrap=True), 2)
    message += '-----------------------------\n\n'
    try:
        log_file = open(log_path, 'a+')
        log_file.writelines(message)
        log_file.close()
    except:
        pass

def get_table_update_time(tablename, run_on_slave=False):
    """Return update time of TABLENAME.  TABLENAME can contain
       wildcard `%' in which case we return the maximum update time
       value.
    """
    res = run_sql("SELECT UPDATE_TIME FROM information_schema.tables " +
                  "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s",
                  (CFG_DATABASE_NAME, tablename), run_on_slave=run_on_slave)
    if res:
        return str(max([d[0] for d in res]))

    return ''


def get_table_status_info(tablename, run_on_slave=False):
    """Return table status information on TABLENAME.  Returned is a
       dict with keys like Name, Rows, Data_length, Max_data_length,
       etc.  If TABLENAME does not exist, return empty dict.
    """
    res = run_sql("SELECT TABLE_NAME, TABLE_ROWS, DATA_LENGTH, MAX_DATA_LENGTH, CREATE_TIME, UPDATE_TIME " +
                  "FROM information_schema.tables WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s",
                  (CFG_DATABASE_NAME, tablename), run_on_slave=run_on_slave)
    if res:
        status_info = dict(zip(['Name', 'Rows', 'Data_length', 'Max_data_length', 'Create_time', 'Update_time'], res[0]))
        return status_info

    return {}

def serialize_via_marshal(obj):
    """Serialize Python object via marshal into a compressed string."""
    return compress(marshal.dumps(obj))

@gcfix
def deserialize_via_marshal(astring):
    """Decompress and deserialize string into a Python object via marshal."""
    return marshal.loads(decompress(astring))

def wash_table_column_name(colname):
    """
    Evaluate table-column name to see if it is clean.
    This function accepts only names containing [a-zA-Z0-9_].

    @param colname: The string to be checked
    @type colname: str

    @return: colname if test passed
    @rtype: str

    @raise Exception: Raises an exception if colname is invalid.
    """
    if re.search('[^\w]', colname):
        raise Exception('The table column %s is not valid.' % repr(colname))
    return colname

def real_escape_string(unescaped_string, run_on_slave=False):
    """
    Escapes special characters in the unescaped string for use in a DB query.

    @param unescaped_string: The string to be escaped
    @type unescaped_string: str

    @return: Returns the escaped string
    @rtype: str
    """
    dbhost = CFG_DATABASE_HOST
    if run_on_slave and CFG_DATABASE_SLAVE:
        dbhost = CFG_DATABASE_SLAVE
    connection_object = _db_login(dbhost)
    escaped_string = connection_object.escape_string(unescaped_string)
    return escaped_string
