"""
Validator
====================================
Component names in KiCad files are freetext fields, therefore we try to validate whether or not the specified components
are actual existing components that can be bought. We do this by searching components in the DigiKey database, and
evaluating the number of results. Components that can not be found, or that return more than 10 results are considered
as non-existent or too general.

The validator downloads existing components from the AISLER API. AISLER retrieves this info from DigiKey, so we are
building a database of parts that can be bought at DigiKey. Because AISLER's rate limit at DK is not very big, we only
download their cache. Over time, this will allow us a big enough database of components to validate parts. In the
future, it might be better to immediately validate with DK, if we can get a big enough rate limit.
"""

import argparse
import json
import pickle
from configparser import ConfigParser
import os
from datetime import datetime, timedelta
import requests
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker, scoped_session
from tqdm import tqdm
import time
import jellyfish
import signal

from models.base import Base
from models.files import File
from models.items import Item
from models.part import Part

graceful_exit = False
original_sigint_handler = signal.getsignal(signal.SIGINT)
original_sigterm_handler = signal.getsignal(signal.SIGTERM)


def graceful_skip(_signo, _stack_frame):
    """
    Handler for gracefully terminating the search.

    :param _signo: Signal Number
    :param _stack_frame: Stack Frame
    :return: -
    """
    print("Received Stop Signal. Gracefully aborting!")
    global graceful_exit
    graceful_exit = True


def register_graceful_exit():
    """
    Register handling the graceful termination

    :return: -
    """
    signal.signal(signal.SIGTERM, graceful_skip)
    signal.signal(signal.SIGINT, graceful_skip)


def deregister_graceful_exit():
    """
    de-register the handlers for graceful termination

    :return: -
    """
    signal.signal(signal.SIGTERM, original_sigterm_handler)
    signal.signal(signal.SIGINT, original_sigint_handler)


# read the configuration
config = ConfigParser()

# check whether we have a custom config file
if os.path.exists('config/parser.config'):
    config.read('config/parser.config')
else:
    config.read('config/default_parser.config')

try:
    AISLER_CLIENT_ID = os.environ['AISLER_CLIENT_ID']
except KeyError:
    AISLER_CLIENT_ID = config['AISLER_API']['Client-ID']

try:
    AISLER_AUTH_TOKEN = os.environ['AISLER_AUTH_TOKEN']
except KeyError:
    AISLER_AUTH_TOKEN = config['AISLER_API']['Authorization']

# Initialize SQL session
Session = None
try:
    # Init SQLAlchemy
    engine = create_engine(config["DATABASE"]["database-uri"])
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    Session = scoped_session(session_factory)
except Exception as e:
    print("Failed to initialize SQL Session")
    print(e)
    exit(-4)


def parse_and_insert_response_parts(parts):
    """
    Parse the response parts and insert into DB

    :param parts: Parts JSON form API
    :return: -
    """

    # iterate though the parts in response
    for part in parts:
        aisler_part = {}

        # aisler_part["uuid"] = str(uuid.uuid4())
        aisler_part["aisler_id"] = part["id"]
        aisler_part["mpn"] = part["attributes"]["mpn"]
        aisler_part["manufacturer"] = part["attributes"]["manufacturer"]
        aisler_part["datasheet"] = part["attributes"]["datasheet"]
        aisler_part["description"] = part["attributes"]["description"]

        session = Session()

        try:
            db_part = Part(
                # uuid=aisler_part["uuid"],
                aisler_id=aisler_part["aisler_id"],
                mpn=aisler_part["mpn"],
                manufacturer=aisler_part["manufacturer"],
                description=aisler_part["description"],
                datasheet=aisler_part["datasheet"]
            )

            # get items from db that have the same aisler ID (there should only be one hence use first)
            in_db = session.query(Part).filter(Part.aisler_id == db_part.aisler_id).first()

            # if a part in the DB has been found, update it accordingly, else add
            if in_db is not None:
                # Note: sqlalchemy seems to be already so clever and updates the part if we assign it...
                in_db = db_part
            else:
                # add part to db
                session.add(db_part)

            # commit changes
            session.commit()
        except IntegrityError:
            print("Part already in DB.")
        except Exception as e:
            raise e
        finally:
            session.close()


def save_status(next_urls, meta, finished=False):
    """
    Save the current query status in a pickle file.
    If finished=True, we save the current date as reference.

    :param next_urls: the list of next urls to query
    :param meta: the meta info dict
    :param finished: False if still running, True if just finished
    :return:
    """
    with open('validator_status.pickle', 'wb') as pickle_file:
        status_dict = {
            'finished': None,
            'meta': meta,
            'next_urls': next_urls
        }

        if finished:
            status_dict.update(
                {
                    'finished': datetime.now(),
                }
            )

        pickle.dump(status_dict, pickle_file, pickle.HIGHEST_PROTOCOL)


def fetch_status():
    """
    Fetches the status from the pickle file for further processing and resume.

    :return: (next_urls, meta, finish)
        - next_urls: list of pending urls to query
        - meta: dict with meta information
        - finish: None, if still crawling, or date of last finished crawl
    """
    if os.path.exists('validator_status.pickle'):
        with open('validator_status.pickle', 'rb') as pickle_file:
            try:
                data = pickle.load(pickle_file)
                return data["next_urls"], data["meta"], data["finished"]
            except Exception as e:
                print(e)
                print("Error opening Pickle status file...")

    return None, None, None


def build_parts_db():
    """
    Mehtod that builds the Parts DB by crawling the AISLER Cache API

    :return: -
    """
    print("Building Parts Database...")
    next_urls, meta, finished = fetch_status()

    if finished is not None:
        # The next crawl should be at least one week away!
        next_crawl_at = finished + timedelta(weeks=1)

        # check whether we should continue with a new run
        if next_crawl_at > datetime.now():
            return
        else:
            # we should re run from scratch. rerun
            meta = None

    if meta is None:
        meta = {
            "total": -1,  # total items
            "offset": -1,  # already parsed items
            "limit": -1,  # page limit (fixed 50 currently)
        }

    if next_urls is None:
        # list of next urls to crawl
        next_urls = [config["AISLER_API"]["Parts_Url"]]

    # init progress bar
    progress = tqdm(next_urls, total=1, desc="Items Downloaded")

    # update with last progress if we had one
    if meta["offset"] >= 0:
        progress.update(meta["offset"])

    # while we still have something to crawl
    while len(next_urls) > 0 and not graceful_exit:
        url = next_urls.pop(0)
        print(f"Fetching: {url}")

        headers = {
            'Client-ID': AISLER_CLIENT_ID,
            'Authorization': AISLER_AUTH_TOKEN
        }

        # fetch API data
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            print(response.status_code)
            print(response.text)
            print(response.headers)
            exit(-5)

        # read JSON data from response
        response_json = json.loads(response.text)

        META_KEY = "meta"
        OFFSET_KEY = "offset"
        if META_KEY in response_json.keys():
            response_meta = response_json[META_KEY]

            if meta[OFFSET_KEY] >= response_meta[OFFSET_KEY]:
                print("Loop detected. New offset is smaller or equal to last one. Exit.")
                exit(-10)

            # update progress bar
            meta.update(response_meta)
            progress.total = meta["total"]
            progress.refresh()

        response_data = None
        DATA_KEY = "data"
        if DATA_KEY in response_json.keys():
            response_data = response_json[DATA_KEY]
            progress.update(len(response_data))

        LINK_KEY = "links"
        NEXT_KEY = "next"
        if LINK_KEY in response_json.keys():
            response_link = response_json[LINK_KEY]

            if response_link[NEXT_KEY] is not None:
                next_url = response_link[NEXT_KEY]

                if url == next_url:
                    print("Loop detected. New next URL same w/ current. Exit.")
                    exit(-6)

                next_urls.append(next_url)

        try:
            parse_and_insert_response_parts(response_data)
        except Exception as e:
            print(e)
            exit(-7)
        finally:
            save_status(next_urls, meta)
            sleep_time = 20  # secs
            print(f"Sleeping for {sleep_time}s until continuation...")
            time.sleep(sleep_time)  # wait 5 secs until continue

    # we have run through once. Mark finished...
    progress.close()
    if not graceful_exit:
        save_status(None, None, True)
    else:
        save_status(next_urls, meta)


def match_parts(query_item, related_parts):
    """
    Matching algorithm to match found parts to an kicad module item

    :param query_item: the item to be matched
    :param related_parts: list of possible parts that match
    :return: best matching Part
    """
    best = {"part": None, "dist": None}
    for related_part in related_parts:
        # use levenshtein distance to measure how far apart the query term is to the mpn
        dist = jellyfish.levenshtein_distance(query_item.Item.value, related_part.mpn)

        if best["dist"] is None or dist < best["dist"]:
            best["part"] = related_part
            best["dist"] = dist
    return best["part"]


def validate_parts():
    """
    Validate found Items by matching them with possible parts.

    :return: -
    """
    print("Validating Parts against local DB...")
    session = Session()

    queried_items = session.query(Item, File).filter(Item.file_id == File.id).all()

    try:
        for item in tqdm(queried_items):
            related_parts = session.query(Part).filter(
                Part.mpn.ilike(f"%{item.Item.value}%")
            ).all()

            if len(related_parts) > 0:
                best_match = match_parts(item, related_parts)
                item.Item.part_id = best_match.id

                session.commit()
    except Exception as e:
        raise e
    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Crawler for Parts DB and validator of found parts.')
    parser.add_argument('--validate-only', "-v", action="store_true",
                        help='Skip DB creation and validate only!')
    args = parser.parse_args()

    if not args.validate_only:
        register_graceful_exit()
        build_parts_db()
        deregister_graceful_exit()

    validate_parts()
