# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2011, 2018, 2019 CERN.
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
WebAuthorProfile web interface logic and URL handler
"""

# pylint: disable=W0105
# pylint: disable=C0301
# pylint: disable=W0613

from sys import hexversion
from urllib import urlencode
from datetime import datetime, timedelta

from invenio import webinterface_handler_config
from invenio.bibauthorid_webauthorprofileinterface import is_valid_canonical_id, \
    is_valid_bibref, get_person_id_from_paper, get_person_id_from_canonical_id, \
    search_person_ids_by_name, get_papers_by_person_id, get_person_redirect_link, \
    author_has_papers, get_authors_by_name
from invenio.bibauthorid_webapi import history_log_visit

from invenio.config import CFG_BASE_URL, CFG_LABS_HOSTNAME

from invenio.webauthorprofile_corefunctions import get_pubs, get_person_names_dicts, \
    get_institute_pubs, get_pubs_per_year, get_coauthors, get_summarize_records, \
    get_total_downloads, get_kwtuples, get_fieldtuples, get_veryfy_my_pubs_list_link, \
    get_hepnames_data, get_self_pubs, get_collabtuples, get_internal_publications, \
    get_external_publications, expire_all_cache_for_person, get_person_oldest_date, \
    get_datasets, get_canonical_name_of_author
from invenio.webauthorprofile_config import deserialize

from invenio.bibauthorid_general_utils import get_doi_url, get_arxiv_url, get_inspire_record_url
from invenio.webpage import page
from invenio.webinterface_handler import wash_urlargd, WebInterfaceDirectory
from invenio.urlutils import redirect_to_url
from invenio.jsonutils import json_unicode_to_utf8
from invenio.bibauthorid_templates import WebProfileMenu, WebProfilePage
from invenio.bibauthorid_webinterface import WebInterfaceAuthorTicketHandling
import invenio.bibauthorid_webapi as webapi
from invenio.bibauthorid_dbinterface import get_canonical_name_of_author
from invenio.bibauthorid_config import CFG_BIBAUTHORID_ENABLED, AID_VISIBILITY
from invenio.bibformat import format_record
import invenio.template
import cProfile, pstats, cStringIO

websearch_templates = invenio.template.load('websearch')
webauthorprofile_templates = invenio.template.load('webauthorprofile')
bibauthorid_template = invenio.template.load('bibauthorid')

from invenio.search_engine import page_end, perform_request_search
JSON_OK = False

if hexversion < 0x2060000:
    try:
        import simplejson as json
        JSON_OK = True
    except ImportError:
        # Okay, no Ajax app will be possible, but continue anyway,
        # since this package is only recommended, not mandatory.
        JSON_OK = False
else:
    try:
        import json
        JSON_OK = True
    except ImportError:
        JSON_OK = False

from webauthorprofile_config import CFG_SITE_LANG, CFG_SITE_URL

RECOMPUTE_ALLOWED_DELAY = timedelta(minutes=30)

BOOTSTRAP_WIDTH_WHOLE = 12
BOOTSTRAP_WIDTH_HALF = 6

def wrap_json_req_profiler(func):

    def json_req_profiler(self, req, form):
        if "ajaxProfile" in form:
            profiler = cProfile.Profile()
            return_val = profiler.runcall(func, self, req, form)

            results = cStringIO.StringIO()
            stats = pstats.Stats(profiler, stream=results)
            stats.sort_stats('cumulative')
            stats.print_stats(100)

            json_data = json.loads(return_val)
            json_data.update({"profilerStats": "<pre style='overflow: scroll'>" + results.getvalue() + "</pre>"})
            return json.dumps(json_data)

        else:
            return func(self, req, form)

    return json_req_profiler

class WebAuthorPages(WebInterfaceDirectory):
    '''
    Handles webauthorpages /author/profile/
    '''
    _exports = ['',
                ('affiliations', 'create_authorpage_affiliations'),
                'create_authorpage_authors_pubs',
                ('citations-summary', 'create_authorpage_citations'),
                ('co-authors', 'create_authorpage_coauthors'),
                ('collaborations', 'create_authorpage_collaborations'),
                ('papers-summary', 'create_authorpage_combined_papers'),
                ('subject-categories', 'create_authorpage_fieldcodes'),
                ('hepnames', 'create_authorpage_hepdata'),
                ('keywords', 'create_authorpage_keywords'),
                ('name-variants', 'create_authorpage_name_variants'),
                'create_authorpage_pubs',
                ('publications-graph', 'create_authorpage_pubs_graph'),
                ('publications-list', 'create_authorpage_pubs_list'),
                ('announcements', 'create_announcements_box')]


    def __init__(self, identifier=None):
        '''
        Constructor of the web interface.

        @param identifier: identifier of an author. Can be one of:
            - an author id: e.g. "14"
            - a canonical id: e.g. "J.R.Ellis.1"
            - a bibrefrec: e.g. "100:1442,155"
        @type identifier: str
        '''
        self.person_id = -1   # -1 is a non valid author identifier
        self.cid = None
        self.original_search_parameter = identifier

        self._prepare_render_variables()

        if (not CFG_BIBAUTHORID_ENABLED or
            identifier is None or
            not isinstance(identifier, str)):
            return

        # check if it's a canonical id: e.g. "J.R.Ellis.1"
        pid = int(get_person_id_from_canonical_id(identifier))
        if pid >= 0:
            self.person_id = pid
            self.cid = get_person_redirect_link(self.person_id)
            return

        # check if it's an author id: e.g. "14"
        try:
            self.person_id = int(identifier)
            cid = get_person_redirect_link(pid)
            # author may not have a canonical id
            if is_valid_canonical_id(cid):
                self.cid = cid
            return
        except ValueError:
            pass

        # check if it's a bibrefrec: e.g. "100:1442,155"
        if is_valid_bibref(identifier):
            pid = int(get_person_id_from_paper(identifier))
            if pid >= 0:
                self.person_id = pid
                self.cid = get_person_redirect_link(self.person_id)
                return

    def _lookup(self, component, path):
        '''
        This handler parses dynamic URLs:
            - /author/profile/1332 shows the page of author with id: 1332
            - /author/profile/100:5522,1431 shows the page of the author
              identified by the bibrefrec: '100:5522,1431'
        '''
        if not component in self._exports:
            return WebAuthorPages(component), path

    def _prepare_render_variables(self):
        '''
        Computes variables for rendering the profile.
        Bootstrap's grid model divides the whole screen into 12 units.
        Here we compute width in units for some of the elements.
        It allows customising profile page.
        '''

        if AID_VISIBILITY['coauthors']:
            coauthors_width = (BOOTSTRAP_WIDTH_WHOLE - BOOTSTRAP_WIDTH_HALF *
                               AID_VISIBILITY['papers'])
        else:
            coauthors_width = 0

        if AID_VISIBILITY['subject_categories']:
            subject_categories_width = (BOOTSTRAP_WIDTH_WHOLE -
                                        BOOTSTRAP_WIDTH_HALF *
                                        AID_VISIBILITY['frequent_keywords'])
        else:
            subject_categories_width = 0

        # Indicates whether a box for publications is visible or not
        publication_box = (AID_VISIBILITY['publications'] or
                           AID_VISIBILITY['datasets'] or
                           AID_VISIBILITY['external'])

        # Those three indicate if a column in 'large' view is visible.
        personal_info_column = (AID_VISIBILITY['personal_details'] or
                                AID_VISIBILITY['name_variants'] or
                                AID_VISIBILITY['affiliations'] or
                                AID_VISIBILITY['collaborations'])
        publications_column = (publication_box or
                               AID_VISIBILITY['coauthors'] or
                               AID_VISIBILITY['papers'] or
                               AID_VISIBILITY['subject_categories'] or
                               AID_VISIBILITY['frequent_keywords'])
        stats_column = (AID_VISIBILITY['citations_summary'] or
                        AID_VISIBILITY['publications_graph'])

        # Here we compute width for three columns in 'large' view. They
        # must sum up to 12.
        if publications_column and personal_info_column and stats_column:
            columns_large_width = {
                'personal_info' : '3',
                'publications' : '5',
                'stats' : '4'
            }
        elif publications_column and personal_info_column:
            columns_large_width = {
                'personal_info' : '5',
                'publications' : '7',
                'stats' : '0'
            }
        elif publications_column and stats_column:
            columns_large_width = {
                'personal_info' : '0',
                'publications' : '7',
                'stats' : '5'
            }
        elif personal_info_column and stats_column:
            columns_large_width = {
                'personal_info' : '6',
                'publications' : '0',
                'stats' : '6'
            }
        elif publications_column:
            columns_large_width = {
                'personal_info' : '0',
                'publications' : str(BOOTSTRAP_WIDTH_WHOLE),
                'stats' : '0'
            }
        elif personal_info_column:
            columns_large_width = {
                'personal_info' : str(BOOTSTRAP_WIDTH_WHOLE),
                'publications' : '0',
                'stats' : '0'
            }
        elif stats_column:
            columns_large_width = {
                'personal_info' : '0',
                'publications' : '0',
                'stats' : str(BOOTSTRAP_WIDTH_WHOLE)
            }
        else:
            raise ValueError("You can not disable all author identification " +
                             "module's fields in bibauthorid_config.py!")

        # This dictionary will be passed to html template
        self.render_width_dict = {
            'coauthors': str(coauthors_width),
            'papers': str(BOOTSTRAP_WIDTH_WHOLE - coauthors_width),
            'subject_categories': str(subject_categories_width),
            'frequent_keywords': str(BOOTSTRAP_WIDTH_WHOLE -
                                     subject_categories_width),
            'publication_box': publication_box,
            'columns_large': columns_large_width
        }


    def _is_profile_owner(self, pid):
        return self.person_id == int(pid)

    def _is_admin(self, pinfo):
        return pinfo['ulevel'] == 'admin'

    def _possible_to_recompute(self, pinfo):
        oldest_cache_date = self.last_computed()
        delay = datetime.now() - oldest_cache_date
        if self._is_admin(pinfo) or (delay > RECOMPUTE_ALLOWED_DELAY):
            return True
        else:
            return False


    def __call__(self, req, form):
        '''
        Serves the main person page.
        Will use the object's person id to get a person's information.

        @param req: apache request object
        @type req: apache request object
        @param form: POST/GET variables of the request
        @type form: dict

        @return: a full page formatted in HTML
        @rtype: str
        '''
        if not CFG_BIBAUTHORID_ENABLED:
            self.person_id = self.original_search_parameter
            return self.index(req, form)

        argd = wash_urlargd(form, {'ln': (str, CFG_SITE_LANG),
                                   'recid': (int, -1),
                                   'verbose': (int, 0)})

        ln = argd['ln']
        verbose = argd['verbose']
        url_args = dict()
        if ln != CFG_SITE_LANG:
            url_args['ln'] = ln
        if verbose:
            url_args['verbose'] = str(verbose)
        encoded = urlencode(url_args)
        if encoded:
            encoded = '?' + encoded

        if self.cid is not None and self.original_search_parameter != self.cid:
            return redirect_to_url(req, '%s/author/profile/%s%s' % (CFG_SITE_URL, self.cid, encoded))

        # author may have only author identifier and not a canonical id
        if self.person_id > -1:
            return self.index(req, form)

        recid = argd['recid']

        if recid > -1:
            possible_authors = get_authors_by_name(self.original_search_parameter,
                                                   limit_to_recid=recid)

            if len(possible_authors) == 1:
                self.person_id = possible_authors.pop()
                self.cid = get_person_redirect_link(self.person_id)
                redirect_to_url(req, '%s/author/profile/%s%s' % (CFG_SITE_URL, self.cid, encoded))

        encoded = urlencode(url_args)
        if encoded:
            encoded = '&' + encoded

        return redirect_to_url(req, '%s/author/search?q=%s%s' %
                               (CFG_SITE_URL, self.original_search_parameter, encoded))


    def index(self, req, form):
        '''
        Serve the main person page.
        Will use the object's person id to get a person's information.

        @param req: apache request object
        @type req: apache request object
        @param form: POST/GET variables of the request
        @type form: dict

        @return: a full page formatted in HTML
        @return: str
        '''

        webapi.session_bareinit(req)
        session = webapi.get_session(req)
        pinfo = session['personinfo']
        ulevel = pinfo['ulevel']

        argd = wash_urlargd(form, {'ln': (str, CFG_SITE_LANG),
                                   'recompute': (int, 0),
                                   'verbose': (int, 0),
                                   'trial': (str, None)})

        ln = argd['ln']
        debug = "verbose" in argd and argd["verbose"] > 0

        # Create Page Markup and Menu
        try:
            int(self.person_id)
        except ValueError:
            cname = self.person_id
        else:
            cname = webapi.get_canonical_id_from_person_id(self.person_id)
        menu = WebProfileMenu(str(cname), "profile", ln, self._is_profile_owner(pinfo['pid']), self._is_admin(pinfo))


        profile_page = WebProfilePage("profile", webapi.get_longest_name_from_pid(self.person_id))
        profile_page.add_profile_menu(menu)

        if 'form_email' in pinfo:
            gFormEmail = pinfo['form_email']
        else:
            gFormEmail = ""

        profile_page.add_bootstrapped_data(json.dumps({
            "backbone": """
            (function(ticketbox) {
                var app = ticketbox.app;
                app.userops.set(%s);
                app.bodyModel.set({userLevel: "%s"});
            })(ticketbox);""" % (WebInterfaceAuthorTicketHandling.bootstrap_status(pinfo, "user"), ulevel),
            "other": "var gUserLevel = '%s'; var gFormEmail = '%s';" % (ulevel, gFormEmail)
        }))

        if debug:
            profile_page.add_debug_info(pinfo)

        last_computed = str(self.last_computed())
        context = {
            'person_id': self.person_id,
            'last_computed': last_computed,
            'citation_fine_print_link': "%s/help/citation-metrics" % CFG_BASE_URL,
            'search_form_url': "%s/author/search" % CFG_BASE_URL,
            'possible_to_recompute': self._possible_to_recompute(pinfo)
        }

        verbose = argd['verbose']
        url_args = dict()
        if ln != CFG_SITE_LANG:
            url_args['ln'] = ln
        if verbose:
            url_args['verbose'] = str(verbose)
        encoded = urlencode(url_args)
        if encoded:
            encoded = '&' + encoded

        if CFG_BIBAUTHORID_ENABLED:
            if self.person_id < 0:
                return redirect_to_url(req, '%s/author/search?q=%s%s' %
                                       (CFG_SITE_URL, self.original_search_parameter, encoded))
        else:
            self.person_id = self.original_search_parameter
            profile_page.menu = None

        assert not form.has_key('jsondata'), "Content type should be only text/html."

        full_name = webapi.get_longest_name_from_pid(self.person_id)
        page_title = '%s - Profile' % full_name

        if argd['recompute'] and req.get_method() == 'POST':
            expire_all_cache_for_person(self.person_id)
            context['last_computed'] = str(datetime.now().replace(microsecond=0))

        history_log_visit(req, 'profile', pid=self.person_id)

        meta = profile_page.get_head()

        context["visible"] = AID_VISIBILITY
        context["element_width"] = self.render_width_dict
        hndata = get_hepnames_data(self.person_id)
        if hndata[1] is True:
            context["hnrecid"] = get_hepnames_data(self.person_id)[0].get('record', {}).get('record_id')

        body = profile_page.get_wrapped_body("profile_page", context)
        return page(title=page_title,
                    metaheaderadd=meta.encode('utf-8'),
                    body=body.encode('utf-8'),
                    req=req,
                    language=ln,
                    show_title_p=False)

    @wrap_json_req_profiler
    def create_authorpage_name_variants(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                namesdict, namesdictStatus = get_person_names_dicts(person_id)
                if not namesdict:
                    namesdict = dict()
                try:
                    db_names_dict = namesdict['db_names_dict']
                except (IndexError, KeyError):
                    db_names_dict = dict()

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                json_response = {'status': namesdictStatus, 'html': webauthorprofile_templates.tmpl_author_name_variants_box(db_names_dict, bibauthorid_data, ln='en', add_box=False, loading=not db_names_dict)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_combined_papers(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                pubs, pubsStatus = get_pubs(person_id)
                if not pubs:
                    pubs = list()

                selfpubs, selfpubsStatus = get_self_pubs(person_id)
                if not selfpubs:
                    selfpubs = list()

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                totaldownloads, totaldownloadsStatus = get_total_downloads(person_id)
                if not totaldownloads:
                    totaldownloads = 0

                json_response = {'status': selfpubsStatus, 'html': webauthorprofile_templates.tmpl_papers_with_self_papers_box(pubs, selfpubs, bibauthorid_data, totaldownloads, ln='en', add_box=False, loading=not selfpubsStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_keywords(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                kwtuples, kwtuplesStatus = get_kwtuples(person_id)
                if kwtuples:
                    pass
                    # kwtuples = kwtuples[0:MAX_KEYWORD_LIST]
                else:
                    kwtuples = list()

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                json_response = {'status': kwtuplesStatus, 'html': webauthorprofile_templates.tmpl_keyword_box(kwtuples, bibauthorid_data, ln='en', add_box=False, loading=not kwtuplesStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_fieldcodes(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                fieldtuples, fieldtuplesStatus = get_fieldtuples(person_id)
                if fieldtuples:
                    pass
                    # fieldtuples = fieldtuples[0:MAX_FIELDCODE_LIST]
                else:
                    fieldtuples = list()

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                json_response = {'status': fieldtuplesStatus, 'html': webauthorprofile_templates.tmpl_fieldcode_box(fieldtuples, bibauthorid_data, ln='en', add_box=False, loading=not fieldtuplesStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_affiliations(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                author_aff_pubs, author_aff_pubsStatus = get_institute_pubs(person_id)
                if not author_aff_pubs:
                    author_aff_pubs = dict()

                json_response = {'status': author_aff_pubsStatus, 'html': webauthorprofile_templates.tmpl_affiliations_box(author_aff_pubs, ln='en', add_box=False, loading=not author_aff_pubsStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_coauthors(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                coauthors, coauthorsStatus = get_coauthors(person_id)
                if not coauthors:
                    coauthors = dict()

                json_response = {'status': coauthorsStatus, 'html': webauthorprofile_templates.tmpl_coauthor_box(bibauthorid_data, coauthors, ln='en', loading=not coauthorsStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_citations(self, req, form):
        if 'jsondata' in form:
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)

            if 'personId' in json_data:
                person_id = json_data['personId']

                citation_data, cache_status = get_summarize_records(person_id)
                records, records_cache_status = get_pubs(person_id)
                datasets = get_datasets(person_id)
                if datasets and datasets[0]:
                    datasets = len(datasets[0])
                else:
                    datasets = 0

                citations = {'breakdown_categories': ['Renowned papers (500+)', 'Famous papers (250-499)',
                                                      'Very well-known papers (100-249)',
                                                      'Well-known papers (50-99)',
                                                      'Known papers (10-49)', 'Less known papers (1-9)',
                                                      'Unknown papers (0)']}

                content = "Data not ready. Please wait..."
                if cache_status and citation_data and records and records_cache_status:
                    citations['papers_num'] = len(records)
                    try:
                        citations['papers'], citations['data'] = citation_data[0]
                    except IndexError:
                        pass

                    result = get_canonical_name_of_author(person_id)
                    if result:
                        canonical_name = result[0][0]
                    else:
                        canonical_name = ""
                    content = WebProfilePage.render_citations_summary_content(citations, canonical_name, datasets)
                elif not citation_data and not records:
                    content = "No citations data."

                json_response = {'status': (cache_status and records_cache_status), 'html': content}

                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_pubs_graph(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                pubs_per_year, pubs_per_yearStatus = get_pubs_per_year(person_id)
                if not pubs_per_year:
                    pubs_per_year = dict()
                securelinks = req.is_https()
                json_response = {'status': pubs_per_yearStatus, 'html': webauthorprofile_templates.tmpl_graph_box(pubs_per_year, ln='en', loading=not pubs_per_yearStatus, https=securelinks)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_hepdata(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)

            webapi.session_bareinit(req)
            session = webapi.get_session(req)
            ulevel = session['personinfo']['ulevel']

            if json_data.has_key('personId'):
                person_id = json_data['personId']

                context, hepdictStatus = get_hepnames_data(person_id)
                if not hepdictStatus:
                    return json.dumps({'status': False, 'html': ''})

                context.update({
                    'cname': webapi.get_canonical_id_from_person_id(person_id),
                    'link_to_record': ulevel == "admin",
                    'hepnames_link': "%s/%s/" % (CFG_BASE_URL, "record"),
                    'new_record_link': 'https://%s/submissions/authors?bai=%s' % (CFG_LABS_HOSTNAME, webapi.get_canonical_id_from_person_id(person_id)),
                    'update_link_prefix': "https://%s/submissions/authors/" % CFG_LABS_HOSTNAME,
                    'update_link_suffix': "",
                    'profile_link': "%s/%s" % (CFG_BASE_URL, "author/profile/")
                })

                content = WebProfilePage.render_template('personal_details_box', context)

                json_response = {'status': hepdictStatus, 'html': content}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_collaborations(self, req, form):
        if form.has_key('jsondata'):
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if json_data.has_key('personId'):
                person_id = json_data['personId']

                collab, collabStatus = get_collabtuples(person_id)

                person_link, person_linkStatus = get_veryfy_my_pubs_list_link(person_id)
                bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': None}
                if person_link and person_linkStatus:
                    bibauthorid_data = {'is_baid': True, 'pid': person_id, 'cid': person_link}

                json_response = {'status': collabStatus, 'html': webauthorprofile_templates.tmpl_collab_box(collab, bibauthorid_data, ln='en', add_box=False, loading=not collabStatus)}
                req.content_type = 'application/json'
                return json.dumps(json_response)

    @wrap_json_req_profiler
    def create_authorpage_pubs_list(self, req, form):

        def get_unsorted_datasets(items):
            return [(title, get_inspire_record_url(recid),
                     recid) for recid, title
                    in items.iteritems()][0:10]

        def get_sorted_internal_pubs(items, order):
            result = []
            current_index = 0
            offset = 0
            while(current_index < 10 and current_index + offset < len(order)):
                recid = order[current_index + offset]
                if recid in items:
                    title = items[recid]
                    result.append((title, get_inspire_record_url(recid),
                                   recid))
                    current_index += 1
                else:
                    offset += 1
            return result

        if 'jsondata' in form:
            json_data = json.loads(str(form['jsondata']))
            json_data = json_unicode_to_utf8(json_data)
            if 'personId' in json_data:
                person_id = json_data['personId']

                try:
                    canonical_name = get_canonical_name_of_author(person_id)[0][0]
                    internal_search_pubs = perform_request_search(p="exactauthor:%s" % canonical_name,
                                                                  sf="earliestdate",
                                                                  so="d")
                except IndexError:
                    canonical_name = None
                    internal_search_pubs = []

                internal_pubs, internal_pubsStatus = get_internal_publications(person_id)
                external_pubs, external_pubsStatus = get_external_publications(person_id)
                datasets_pubs, datasets_pubsStatus = get_datasets(person_id)

                if internal_pubs is not None and internal_pubsStatus is True:
                    internal_pubs = \
                        get_sorted_internal_pubs(internal_pubs, internal_search_pubs)
                else:
                    internal_pubs = list()

                if datasets_pubs is not None and datasets_pubsStatus is True:
                    datasets_pubs_to_display = get_unsorted_datasets(datasets_pubs)
                else:
                    datasets_pubs_to_display = list()

                arxiv_pubs = list()
                doi_pubs = list()
                if external_pubs is not None and external_pubsStatus is True:
                    if 'arxiv' in external_pubs:
                        arxiv_pubs = [(title, get_arxiv_url(arxiv_id), 'arxiv')
                                      for arxiv_id, title
                                      in external_pubs['arxiv'].iteritems()]

                    if 'doi' in external_pubs:
                        doi_pubs = [(title, get_doi_url(doi_id), 'doi')
                        for doi_id, title
                        in external_pubs['doi'].iteritems()]

                external_pubs = arxiv_pubs + doi_pubs

                all_pubs_search_link = "%s/search?p=exactauthor%%3A%s&sf=earliestdate" % (CFG_BASE_URL, canonical_name)

                if datasets_pubs:
                    datasets_pubs_recs = ['recid%%3A%s' % pub for pub in datasets_pubs]
                else:
                    datasets_pubs_recs = list()

                #TODO An operator should be introduced as this will not work for authors with many records.
                datasets_search_link = "%s/search?cc=Data&p=%s" % (CFG_BASE_URL, '+or+'.join(datasets_pubs_recs))

                json_response = {
                    'status': (internal_pubsStatus and external_pubsStatus and datasets_pubsStatus),
                    'html': WebProfilePage.render_publications_box_content({
                        "internal_pubs": internal_pubs,
                        "external_pubs": external_pubs,
                        "datasets": datasets_pubs_to_display,
                        "all_pubs_search_link": all_pubs_search_link,
                        "data_sets_search_link": datasets_search_link,
                        "base_url": CFG_BASE_URL
                    })
                }

                req.content_type = 'application/json'
                return json.dumps(json_response)

    def last_computed(self):
        return get_person_oldest_date(self.person_id)
