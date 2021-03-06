# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2012, 2013, 2015, 2016, 2019 CERN.
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

import os
import random
import re
import string
import time
from datetime import datetime
from tempfile import mkstemp

from invenio.bibedit_utils import get_bibrecord
from invenio.bibrecord import create_record, \
    field_get_subfield_values, \
    print_rec, \
    record_add_field, \
    record_get_field_instances, \
    record_get_field_value, \
    record_get_field_values
from invenio.bibtask import task_get_task_param, \
    task_init, \
    task_low_level_submission, \
    task_set_task_param, \
    task_sleep_now_if_required, \
    task_update_progress, \
    write_message
from invenio.config import CFG_TMPSHAREDDIR, CFG_VERSION
from invenio.dbquery import run_sql
from invenio.docextract_task import fetch_last_updated, \
    store_last_updated
from invenio.intbitset import intbitset
from invenio.search_engine import get_record, \
    perform_request_search
from invenio.sequtils import SequenceGenerator
from invenio.shellutils import split_cli_ids_arg

from unidecode import unidecode

NAME = 'texkey_generator'

DESCRIPTION = """
    Generate TexKeys in records without one

Options:

-i, --id=low[-high]       select according to recID
-a, --all                 check all records

defaults to checking records modified since last runtime
"""

HELP_MESSAGE = """
  Examples:
   (run a daemon job every hour)
      bibtex -s 1h
"""

PREFIX = "bibtex"

TEXKEY_MAXTRIES = 10


class TexkeyNoAuthorError(Exception):
    """ Error raised when the record does not have a main author or a
    collaboration field
    """
    pass


class TexkeyNoYearError(Exception):
    """ Error raised when the record does not have a year field
    """
    pass


def _texkey_random_chars(recid, use_random=False):
    """ Generate the three random chars for the end of the texkey """
    if recid and not use_random:
        # Legacy random char generation from Spires
        texkey_third_part = chr((recid % 26) + 97) + \
            chr(((recid / 26) % 26) + 97) + \
            chr(((recid * 26) % 26) + 97)
    else:
        letters = string.letters.lower()
        texkey_third_part = ""
        for _ in range(3):
            texkey_third_part += random.choice(letters)

    return texkey_third_part


def _get_year(datestring=''):
    """extract 4 digit substring from a datestring"""
    ymatch = re.search(r'\d{4}', datestring)
    if ymatch:
        return ymatch.group()
    else:
        return None


class TexkeySeq(SequenceGenerator):
    """
    texkey sequence generator
    """
    seq_name = 'texkey'

    def _next_value(self, recid=None, xml_record=None, bibrecord=None):
        """
        Returns the next texkey for the given recid

        @param recid: id of the record where the texkey will be generated
        @type recid: int

        @param xml_record: record in xml format
        @type xml_record: string

        @return: next texkey for the given recid.
        @rtype: string

        @raises TexkeyNoAuthorError: No main author (100__a) or collaboration
        (710__g) in the given recid
        """
        if recid is None and xml_record is not None:
            bibrecord = create_record(xml_record)[0]
        elif bibrecord is None:
            bibrecord = get_bibrecord(recid)

        main_author = record_get_field_value(bibrecord,
                                             tag="100",
                                             ind1="",
                                             ind2="",
                                             code="a")

        if not main_author:
            # Try with collaboration name
            main_author = record_get_field_value(bibrecord,
                                                 tag="710",
                                                 ind1="",
                                                 ind2="",
                                                 code="g")
            main_author = "".join([p for p in main_author.split()
                                   if p.lower() != "collaboration"])

        if not main_author:
            # Try with corporate author
            main_author = record_get_field_value(bibrecord,
                                                 tag="110",
                                                 ind1="",
                                                 ind2="",
                                                 code="a")
            if not main_author:
                # Check if it is a Proceedings record
                collections = [collection.lower() for collection in
                               record_get_field_values(bibrecord,
                                                       "980", code="a")]
                if "proceedings" in collections:
                    main_author = "Proceedings"
                else:
                    raise TexkeyNoAuthorError

        # Remove utf-8 special characters
        main_author = unidecode(main_author.decode('utf-8'))
        texkey_first_part = ""
        try:
            texkey_first_part = main_author.split(',')[0]
        except KeyError:
            raise TexkeyNoAuthorError

        # sanitize for texkey use, require at least one letter
        texkey_first_part = re.sub(r'[^-A-Za-z0-9.:/^_;&*<>?|!$+]', '',
                                   texkey_first_part)
        if len(texkey_first_part) < 1 \
           or not re.search(r'[A-Za-z]', texkey_first_part):
            raise TexkeyNoAuthorError

        year = _get_year(
            record_get_field_value(bibrecord,
                                   tag="269",
                                   ind1="",
                                   ind2="",
                                   code="c"))
        if not year:
            year = _get_year(
                record_get_field_value(bibrecord,
                                       tag="260",
                                       ind1="",
                                       ind2="",
                                       code="c"))
            if not year:
                year = _get_year(
                    record_get_field_value(bibrecord,
                                           tag="773",
                                           ind1="",
                                           ind2="",
                                           code="y"))
                if not year:
                    year = _get_year(
                        record_get_field_value(bibrecord,
                                               tag="502",
                                               ind1="",
                                               ind2="",
                                               code="d"))
                    if not year:
                        raise TexkeyNoYearError

        texkey_second_part = ''
        if year:
            texkey_second_part = year

        texkey_third_part = _texkey_random_chars(recid)

        texkey = "%s:%s%s" % \
                 (texkey_first_part, texkey_second_part, texkey_third_part)

        tries = 0
        while self._value_exists(texkey) and tries < TEXKEY_MAXTRIES:
            # Key is already in the DB, generate a new one
            texkey_third_part = _texkey_random_chars(recid, use_random=True)
            texkey = "%s:%s%s" % \
                     (texkey_first_part, texkey_second_part, texkey_third_part)
            tries += 1

        return texkey


# ## Functions related to texkey generator daemon ###

def submit_task(to_submit, mode, sequence_id):
    """ calls bibupload with all records to be modified

    @param to_submit: list of xml snippets to be submitted
    @type: list
    @param mode: mode to be used in bibupload
    @type: list
    @param sequence_id: sequence id to be included in the task_id
    @type: str

    @return: id of the submitted task
    @rtype: int
    """
    (temp_fd, temp_path) = mkstemp(prefix=PREFIX,
                                   dir=CFG_TMPSHAREDDIR)
    temp_file = os.fdopen(temp_fd, 'w')
    temp_file.write('<?xml version="1.0" encoding="UTF-8"?>')
    temp_file.write('<collection>')
    for el in to_submit:
        temp_file.write(el)
    temp_file.write('</collection>')
    temp_file.close()

    return task_low_level_submission('bibupload', PREFIX, '-P', '3', '-I',
                                     sequence_id, '-%s' % mode,
                                     temp_path)


def submit_bibindex_task(to_update, sequence_id):
    """ submits a bibindex task for a set of records

    @param to_update: list of recids to be updated by bibindex
    @type: list
    @param sequence_id: sequence id to be included in the task_id
    @type: str

    @return: id of bibindex task
    @rtype: int
    """
    recids = [str(r) for r in to_update]
    return task_low_level_submission('bibindex', PREFIX, '-I',
                                     sequence_id, '-P', '2', '-w', 'global',
                                     '-i', ','.join(recids))


def wait_for_task(task_id):
    sql = 'select status from schTASK where id = %s'
    while run_sql(sql, [task_id])[0][0] not in ('DONE', 'ACK', 'ACK DONE'):
        task_sleep_now_if_required(True)
        time.sleep(5)


def process_chunk(to_process, sequence_id):
    """ submit bibupload task and wait for it to finish

    @param to_process: list of marcxml snippets
    @type: list
    """
    task_id = submit_task(to_process, 'a', sequence_id)
    return wait_for_task(task_id)


def create_xml(recid, texkey):
    """ Create the marcxml snippet with the new texkey

    @param recid: recid of the record to be updated
    @type: int
    @param texkey: texkey that has been generated
    @type: str

    @return: marcxml with the fields to be record_add_field
    @rtype: str
    """
    record = {}
    record_add_field(record, '001', controlfield_value=str(recid))
    subfields_toadd = [('a', texkey), ('9', 'INSPIRETeX')]
    record_add_field(record, tag='035', subfields=subfields_toadd)
    return print_rec(record)


def fetch_records_modified_since(last_date):
    """Fetch all the recids of records modified since last_date in the system
    """
    return intbitset(run_sql("SELECT id FROM bibrec WHERE"
                             " modification_date>=%s", (last_date, )))


def task_run_core(name=NAME):
    """ Performs a search to find records without a texkey, generates a new
    one and uploads the changes in chunks """
    recids = task_get_task_param('recids')
    if recids:
        start_date = None
        write_message("processing recids from commandline")
    else:
        start_date = datetime.now()
        recids = intbitset()
        recids |= intbitset(perform_request_search(
            p='-035:spirestex -035:inspiretex', cc='HEP'))

        if task_get_task_param('all'):
            write_message("processing all records without texkey")
        else:
            _, last_date = fetch_last_updated(name)
            recids = recids & fetch_records_modified_since(last_date)
            write_message("processing records modified since: %s" % last_date)

    write_message("Found %s records to assign texkeys" % len(recids))
    processed_recids = []
    xml_to_process = []
    for count, recid in enumerate(recids):
        write_message("processing recid %s" % recid)

        # Check that the record does not have already a texkey
        has_texkey = False
        recstruct = get_record(recid)
        for instance in record_get_field_instances(recstruct,
                                                   tag="035",
                                                   ind1="",
                                                   ind2=""):
            try:
                provenance = field_get_subfield_values(instance, "9")[0]
            except IndexError:
                provenance = ""
            try:
                value = field_get_subfield_values(instance, "a")[0]
            except IndexError:
                value = ""
            provenances = ["SPIRESTeX", "INSPIRETeX"]
            if provenance in provenances and value:
                has_texkey = True
                write_message("INFO: Record %s has already texkey %s" %
                              (recid, value))

        if not has_texkey:
            TexKeySeq = TexkeySeq()
            new_texkey = ""
            try:
                new_texkey = TexKeySeq.next_value(recid)
            except TexkeyNoAuthorError:
                write_message(
                    "WARNING: Record %s has no first author or collaboration" %
                    recid)
                continue
            except TexkeyNoYearError:
                write_message("WARNING: Record %s has no year" % recid)
                continue
            write_message("Created texkey %s for record %d" %
                          (new_texkey, recid))
            xml = create_xml(recid, new_texkey)
            processed_recids.append(recid)
            xml_to_process.append(xml)

        task_update_progress("Done %d out of %d." % (count, len(recids)))
        task_sleep_now_if_required()

    # sequence ID to be used in all subsequent tasks
    sequence_id = str(random.randrange(1, 2147483648))
    if xml_to_process:
        process_chunk(xml_to_process, sequence_id)

    # Finally, index all the records processed
    # FIXME: Waiting for sequence id to be fixed
    # if processed_recids:
    #     submit_bibindex_task(processed_recids, sequence_id)

    if start_date:
        store_last_updated(0, start_date, name)

    return True


# pylint: disable-msg=W0613
def parse_option(key, value, opts, args):
    """
    Elaborate task submission parameter.
    """
    if args:
        # There should be no standalone arguments
        raise StandardError("Error: Unrecognised argument '%s'." % args[0])

    if key in ('-i', '--id'):
        recids = task_get_task_param('recids')
        if not recids:
            recids = set()
        task_set_task_param('recids', recids)
        recids.update(split_cli_ids_arg(value))
    elif key in ('-a', '--all'):
        task_set_task_param('all', True)

    return True
# pylint: enable-msg=W0613


def main():
    """Constructs the bibtask."""
    # Build and submit the task
    task_init(authorization_action='runtexkeygeneration',
              authorization_msg="Texkey generator task submission",
              description=DESCRIPTION,
              help_specific_usage=HELP_MESSAGE,
              version="Invenio v%s" % CFG_VERSION,
              specific_params=("ai:", ["all", "id="]),
              task_submit_elaborate_specific_parameter_fnc=parse_option,
              # task_submit_check_options_fnc=check_options,
              task_run_fnc=task_run_core)
