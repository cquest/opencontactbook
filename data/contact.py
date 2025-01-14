#!/usr/bin/env python3
#
# Copyright © Aurélien Pierre - 2022
#
# This file is part of the Open Contact Book project.
#
# Open Contact Book is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Open Contact Book is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

# You should have received a copy of the GNU General Public License along with Open Contact Book.
# If not, see <https://www.gnu.org/licenses/>.

import os
import pandas as pd
import re
import unidecode
import requests
import json
import country_list

import vobject as vo
from data.nominatim import Nominatim
from data.spellcheck import GeoSpellChecker
from urllib.parse import urlencode


def list_vcf_in_directory(directory, progress=None, killswitch=None):
  """
  Thread-safe address book building
  :param progress: Qt Worker Signal to emit progress info
  :param killswitch: Thread-safe boolean stopping the process if == True
  """
  contacts = []
  all_files = os.listdir(directory)
  files_number = len(all_files)
  current_file = 0

  # Walk the directory to find al files
  for file in sorted(all_files):

    # Update the progress bar if any
    if progress is not None:
      progress.emit((current_file, 0, files_number, "Parsing files", "Reading directory"))
      current_file += 1

    # Abort and update the progress bar on killswitch
    if killswitch is not None and killswitch.is_set():
      if progress is not None:
        progress.emit((current_file, 0, current_file, files_number, "cancel", "Reading directory"))
      break

    if file.endswith(".vcf"):

      path = os.path.join(directory, file)
      f = open(path, "r")
      content = f.read()
      f.close()

      content.encode(encoding='UTF-8', errors='strict')

      # Remove accentuated characters in vCard tags
      # Otherwise it makes some vobject fail (actually, the codec lib it uses)
      # Also… what stupid vCard app allows them ???
      regex = r"^([A-ZÉÈÊÀÃ\-\;]+):"
      matches = re.finditer(regex, content, re.MULTILINE)

      for matchNum, match in enumerate(matches):
        tag = match.group(1)
        content = content.replace(tag, unidecode.unidecode(tag))

      # Get the inner of the vcard as a Python dict
      parsed = vo.readOne(content).contents
      parsed["file"] = path
      contacts.append(parsed)

  if progress is not None:
    progress.emit((current_file, files_number, files_number, "Parsing files", "Reading directory"))

  # Collapse this into a database, aka Pandas DataFrame
  data = pd.DataFrame(contacts)

  return data


def cleanup_contact(data, progress=None, killswitch=None):
  """
  Thread-safe address book building
  :param progress: Qt Worker Signal to emit progress info
  """

  if progress is not None:
    progress.emit((0, 0, 3, "Formatting the database", "Prepare data"))

  # Cleanup fully empty columns
  data.dropna(axis=1, how="all", inplace=True)

  # Replace NaN by empty string to not pollute the view
  data.fillna("", inplace=True)

  # Convert to string to be able to regex on it
  data = data.astype("str")

  if progress is not None:
    progress.emit((1, 0, 3, "Cleaning tags", "Prepare data"))

  # Cleanup the Vcard tags
  # 1. Basic tags : [<fn{} name>] -> name
  data.replace(to_replace="\[\<\S+\{\}(.*)\>\]", value=r"\1", regex=True, inplace=True)
  # 2. Tags with nested types : [<adr{'TYPE': ['HOME']} value>] -> "HOME: value"
  data.replace(to_replace="\<\S+\{'?[A-Z]*'?:?\s?\[?'?([A-Z]*)'?\]?\}([^;\]]*)\>", value=r"\1: \2", regex=True, inplace=True)
  # 3. Remove multiple spaces
  data.replace(to_replace="[ ]{2,}", value=" ", regex=True, inplace=True)
  # 4. Remove leading empty elements separated by comas
  data.replace(to_replace="^\s*,\s*[^\S]*", value=" ", regex=True, inplace=True)

  if progress is not None:
    progress.emit((2, 0, 3, "Sorting data", "Prepare data"))

  # Reorder columns in a way that makes sense :
  # 1. start with typical ID and adresses/phone (default vCard fields)
  # 2. end with X-(.*) (custom user-defined fields)
  # 3. fill the middle with the rest of default VCard fields

  original_cols = sorted(list(data.columns.tolist()))
  forced_cols_start = ["categories", "fn", "n", "org", "role", "email", "adr", "tel"]

  for elem in forced_cols_start:
    if elem in original_cols:
      original_cols.remove(elem)

  cols = forced_cols_start + original_cols
  data = data.reindex(columns=cols)

  if progress is not None:
    progress.emit((3, 0, 3, "Sorted", "Prepare data"))

  return data


def get_geoID(data, progress=None, killswitch=None):
  """
  Thread-safe address book building
  :param progress: Qt Worker Signal to emit progress info
  """

  entries = len(data.index)

  if progress is not None:
    progress.emit((0, 0, entries, "Downloading GPS coordinates from nominatim.org…", "Fetch geolocation data"))

  # Try to see if https://nominatim.openstreetmap.org/search is available
  # If not, the DB will be unavailable
  try:
    response = requests.get('https://nominatim.openstreetmap.org/search')
  except:
    if progress is not None:
      progress.emit((0, 0, entries, "nominatim.org can't be reached, geolocation will use the local cache if possible", "Fetch geolocation data"))

  # Get the OSM area ID
  # This can be slow and long since we need to download info from the Nominatim DB
  # However, results are cached, so the next time will run faster
  # To be able to re-use the cache, we actually need to process that single-threaded and sequentially.
  # Also, it wouldn't be nice to DoS the free OSM servers with too many requests per second.
  # See conditions of service use : https://operations.osmfoundation.org/policies/nominatim/
  nominatim = Nominatim()

  # Quick way for reference:
  # Since it's not purely data processing in here, there is no point doing that
  #data['geoID'] = data.apply(lambda row : nominatim.query(re.sub(r"\[[A-Z]+:?\s?\n?([\s\S]*)\]", r"\1", row['adr'].replace("\n", ""))).toJSON(), axis = 1)

  # Get a clean location hint
  data['geohint'] = data['adr']

  # Remove content into parenthesis because it's usually precisions and Nominatim will not be able to parse it
  data['geohint'].replace(to_replace="(?:\(|\@ESCAPEDLEFTPARENTHESIS\@).*(?:\)|\@ESCAPEDRIGHTPARENTHESIS\@)", value=" ", regex=True, inplace=True)

  # Replace dashes and special characters by spaces
  data['geohint'].replace(to_replace="[\-\[\]\{\}]+", value=" ", regex=True, inplace=True)
  data['geohint'].replace(to_replace="[\n\r]+", value=", ", regex=True, inplace=True)

  # Factorize multiple spaces
  data['geohint'].replace(to_replace="\s+", value=" ", regex=True)

  # Remove leading empty elements separated by comas
  data['geohint'].replace(to_replace="^\s*,\s*[^\S]", value="", regex=True, inplace=True)

  # Finally, apply some spell checking
  GeoSpellCheck = GeoSpellChecker(["fr", "en"])

  # Ensure index matches the number of rows, otherwise iterating over rows may not produce the expected result
  data.reset_index(drop=True, inplace=True)

  # Go the slow way to be able to output the progress
  for index, row in data.iterrows():
    if progress is not None:
      progress.emit((index, 0, entries, "Downloading GPS coordinates from nominatim.org…", "Fetch geolocation data"))

    # Abort and update the progress bar on killswitch
    if killswitch is not None and killswitch.is_set():
      if progress is not None:
        progress.emit((index, 0, index, "cancel", "Fetch geolocation data"))
      break

    result = []

    # Decode Unicode
    decoded = unidecode.unidecode(row['geohint'], errors="ignore")

    # Remove illegal characters left-over from bad encodings
    decoded = decoded.replace("\"", "")
    decoded = decoded.replace("(c)", "")
    decoded = decoded.replace("@", "")

    # We may have more than one address per contact (home, office, etc.)
    split = re.split(r"[A-Z]+: ", decoded)

    flag_accurate =  False

    for elem in split:
      elem = elem.strip(" \n\r.;,:").lower()

      # Factorize multiple or orphaned commas
      elem = re.sub(r"(\s?\,)+", ",", elem)

      if len(str(elem)) != 0:
        try:
          query = urlencode({'q': elem,
                             'format': 'json'})
          query = re.sub(r"[\+]+", "+", query).strip("+")
          out = nominatim.fetch_cache_or_web(query)[0]
          result.append(out)

          # We found an exact match
          flag_accurate = True
        except:
          # Third guess: try to remove the country name and replace it by the ISO code
          # Nominatim fails if the country name is not in the same language as the rest
          # of the address,
          # Note: It's not accurate.
          # Ex 1: US State "Georgia" may get identified as the country.
          # Ex 2: If the streetname is a country, the address may also fall in the wrong country
          (country_code, filtered) = GeoSpellCheck.get_country_code_from_text(elem)

          try:
            query = urlencode({'q': filtered,
                               'countrycodes': country_code,
                               'format': 'json'})
            query = re.sub(r"[\+]+", "+", query).strip("+")
            out = nominatim.fetch_cache_or_web(query)[0]
            result.append(out)
          except:
            # Sometimes, the query fails for being too specific
            # In that case, we retry all combinations of the n last elements
            sub_elems = filtered.split(",")
            found = False
            for i in range(0, len(sub_elems) - 1, 1):
              q = sub_elems[-1].strip()
              # Build sub-query with the n-th last elements
              # n = length - i > 1
              for j in range(2, len(sub_elems) - i, +1):
                q = sub_elems[-j].strip() + "," + q

              try:
                query = urlencode({'q': q,
                                   'countrycodes': country_code,
                                   'format': 'json'})
                query = re.sub(r"[\+]+", "+", query).strip("+")
                out = nominatim.fetch_cache_or_web(query)[0]
                result.append(out)
                found = True
                break
              except:
                continue

            if not found:
              try:
                # Try with just the first and last element
                query = urlencode({'q': sub_elems[0].strip(),
                                   'countrycodes': country_code,
                                   'format': 'json'})
                query = re.sub(r"[\+]+", "+", query).strip("+")
                out = nominatim.fetch_cache_or_web(query)[0]
                result.append(out)
              except:
                # Just using one element is simply too risky
                # Abort here
                print(elem, "not found")
    if result:
      data.loc[index, "geoID"] = json.dumps(result)
    else:
      data.loc[index, "geoID"] = "not found"
    data.loc[index, "exactlocation"] = flag_accurate

  if progress is not None:
    progress.emit((index, entries, entries, "cancel", "Fetch geolocation data"))

  return data
