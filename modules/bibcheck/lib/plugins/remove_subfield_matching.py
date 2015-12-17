# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2015 CERN.
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

""" Bibcheck plugin to remove a subfield matching regexp """

import re

def check_record(record, subfield, regexp, subfield_filter=None):
    """
    remove specific subfield matching anchored regexp
    if the matching subfield is the only one in the MARC tag,
    remove the entire MARC tag
    optionally filter by additional subfield
    """
    try:
        subfieldre = re.compile(regexp)
    except re.error:
        return
    if subfield_filter is not None:
        subfield_filter = tuple(subfield_filter)
    else:
        subfield_filter = tuple([None, None])
    for pos, val in record.iterfield(subfield,
                                     subfield_filter=subfield_filter):
        if subfieldre.match(val):
            count = 0
            for spos, _ in record.iterfield(subfield[:5] + '%'):
                if spos[1] == pos[1]:
                    count += 1
            if count > 1:
                record.delete_field((pos[0][0:5], pos[1], pos[2]),
                                    message="deleted %s matching %s"
                                    % (subfield, regexp))
            else:
                record.delete_field((pos[0][0:5], pos[1], None),
                                    message="deleted tag %s matching %s:%s"
                                    % (subfield[0:3], subfield, regexp))
