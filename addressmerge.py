#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Basic modules
import logging as l
l.basicConfig(level=l.DEBUG)
from collections import deque
import copy

# Database modules
import psycopg2

# .osm modules
from imposm.parser import OSMParser
from lxml import etree

class OSMSource(object):
    def __init__(self, database, user, password, host, port, ewkt):
        l.debug('Connecting to postgresql')
        self._conn=psycopg2.connect(database=database, user=user, 
                                    password=password, host=host, 
                                    port=str(port))
        self._conn.set_session(readonly=False, autocommit=False)
        self.ewkt = ewkt
        self.validate_ewkt()
        self.create_table()

    def validate_ewkt(self):
        '''
        This function checks that self.ewkt is a valid EWKT string. It will also fail if
        the DB does not have PostGIS enabled or it could not connect.
        '''
        curs = None
        try:
            curs = self._conn.cursor()
            curs.execute('''SELECT %s::geometry;''', (self.ewkt,))
            curs.connection.rollback()
        except BaseException:
            if curs is not None:
                curs.connection.rollback()
            raise
        finally:
            if curs is not None:
                curs.close()

    def create_table(self):
        curs = None
        try:
            curs = self._conn.cursor()
            curs.execute('''CREATE TEMPORARY TABLE import_addresses
                            (import_id integer,
                            "addr:housenumber" varchar(255),
                            "addr:street" varchar(255),
                            "addr:city" varchar(255));''')

            curs.execute('''CREATE TEMPORARY VIEW local_nodes AS
                            SELECT id, tags, geom FROM nodes
                            WHERE ST_Intersects(geom,%s::geometry);''',
                            (self.ewkt,))

            curs.execute('''CREATE TEMPORARY VIEW local_ways AS
                            SELECT id, tags, linestring AS geom FROM ways
                            WHERE ST_Intersects(linestring,%s::geometry);''',
                            (self.ewkt,))

            curs.execute('''CREATE TEMPORARY VIEW local_mps AS
                            SELECT relation_id AS id,
                                relation_tags AS tags,
                                ST_MakeLine(relation_ways.linestring) AS geom
                            FROM (SELECT relations.id AS relation_id,
                                relations.tags AS relation_tags, geom AS linestring
                            FROM local_ways JOIN relation_members
                            ON (local_ways.id = relation_members.member_id
                            AND relation_members.member_type='W')
                            JOIN relations
                            ON (relation_members.relation_id = relations.id)
                            WHERE relations.tags @> hstore('type','multipolygon'))
                            AS relation_ways
                            GROUP BY relation_id, relation_tags;''')

            curs.execute('''CREATE TEMPORARY TABLE local_all
                            AS SELECT local_nodes.id, 'N'::character(1) AS type,
                                local_nodes.tags, local_nodes.geom FROM local_nodes
                            UNION ALL SELECT local_ways.id, 'W'::character(1) AS type,
                                local_ways.tags, local_ways.geom FROM local_ways
                            UNION ALL SELECT local_mps.id, 'M'::character(1) AS type,
                                local_mps.tags, local_mps.geom FROM local_mps;''')

            curs.execute('''ANALYZE local_all;''');
            curs.connection.commit()
        except BaseException:
            if curs is not None:
                curs.connection.rollback()
            raise
        finally:
            if curs is not None:
                curs.close()

    def load_addresses(self, addresses):
        curs = None
        try:
            curs = self._conn.cursor()
            for (id, tags, _) in addresses:
                curs.execute('''INSERT INTO import_addresses
                                (import_id, "addr:housenumber", "addr:street", "addr:city")
                                VALUES (%s, %s, %s, %s);''',
                                (id, tags['addr:housenumber'], tags['addr:street'], tags['addr:city']))
            curs.execute('''ANALYZE import_addresses;''')
            curs.connection.commit()
        except BaseException:
            if curs is not None:
                curs.connection.rollback()
            raise
        finally:
            if curs is not None:
                curs.close()

    def find_duplicates(self):
        curs = None
        try:
            curs = self._conn.cursor()
            curs.execute('''SELECT DISTINCT ON (import_id) import_id FROM local_all
            JOIN import_addresses
            ON tags -> 'addr:housenumber' = "addr:housenumber"
            AND tags -> 'addr:street' = "addr:street"
            AND tags -> 'addr:city' = "addr:city";''')
            return set(id[0] for id in curs.fetchall())
        except BaseException:
            if curs is not None:
                curs.connection.rollback()
            raise
        finally:
            if curs is not None:
                curs.close()

class ImportDocument(object):
    newNodes = []
    def __init__(self):
        self._parser = OSMParser(nodes_callback=self._parse_nodes)
        self._nodes = deque()

    def parse_osm(self, filename):
        l.debug('Parsing %s', filename)
        self._parser.parse(filename)

    def _parse_nodes(self, nodes):
        for node in nodes:
            self._nodes.append(node)

    def _serialize_node(self, f, node):
        xmlnode = etree.Element('node', {'visible':'true', 'id':str(node[0]), 'lon':str(node[2][0]), 'lat':str(node[2][1])})
        for (k,v) in node[1].items():
            tag = etree.Element('tag',  {'k':k, 'v':v})
            xmlnode.append(tag)

        f.write(etree.tostring(xmlnode))
        f.write('\n')

    def remove_existing(self, source):
        l.debug('Loading addresses')
        source.load_addresses(self._nodes)
        l.debug('Finding duplicates')
        duplicates = source.find_duplicates()
        l.debug('Removing duplicates')
        self._nodes = filter(lambda node: node[0] not in duplicates, self._nodes)
        l.debug('%d duplicates removed', len(duplicates))

    def output_osm(self, f):
        f.write('<?xml version="1.0"?>\n<osm version="0.6" upload="false" generator="addressmerge">\n')
        for node in self._nodes:
            self._serialize_node(f, node)
        f.write('</osm>')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Conflate an address file with existing OSM data')

    # Basic options
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true")
    verbosity.add_argument("-q", "--quiet", action="store_true")

    # Database options
    parser.add_argument('-d', '--dbname', default='osm', help='Database to connect to. Defaults to osm.')
    parser.add_argument('-U', '--username', default='osm', help='Username for database. Defaults to osm.')
    parser.add_argument('--host', default='localhost', help='Hostname for database. Defaults to localhost.')
    parser.add_argument('-p', '--port', default=5432, type=int, help='Port for database. Defaults to 5432.')
    parser.add_argument('-P', '--password', default='osm',  help='Password for database. Defaults to osm.')

    # .osm parser options
    parser.add_argument('--threads', default=None, type=int, help='Threads to use when parsing the input OSM file')
    parser.add_argument('input', help='Input OSM file')
    parser.add_argument('output', type=argparse.FileType('w'), help='Output OSM file')

    # processing options

    parser.add_argument('-e', '--ewkt', type=argparse.FileType('r'), help='Well-known text (EWKT) file with a POLYGON or other area type to search for addresses in')

    args = parser.parse_args()

    existing = OSMSource( database=args.dbname, user=args.username,
                          password=args.password, host=args.host,
                          port=str(args.port),
                          ewkt=args.ewkt.read())

    source = ImportDocument()

    source.parse_osm(args.input)
    source.remove_existing(existing)
    source.output_osm(args.output)
