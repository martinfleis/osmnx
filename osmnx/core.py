################################################################################
# Module: core.py
# Description: Core functions of OSMnx
# License: MIT, see full license in LICENSE.txt
# Web: https://github.com/gboeing/osmnx
################################################################################

import bz2
import geopandas as gpd
import logging as lg
import networkx as nx
import os
import xml.sax
from itertools import groupby
from shapely.geometry import MultiPolygon
from shapely.geometry import Polygon
from . import downloader
from . import projection
from . import settings
from . import simplification
from . import utils
from . import utils_geo
from . import utils_graph
from ._errors import EmptyOverpassResponse
from ._errors import InsufficientNetworkQueryArguments
from ._errors import InvalidDistanceType
from ._version import __version__



def gdf_from_place(query, which_result=1, buffer_dist=None):
    """
    Create a GeoDataFrame from a single place name query.

    Parameters
    ----------
    query : string or dict
        query string or structured query dict to geocode/download
    which_result : int
        max number of results to return and which to process upon receipt
    buffer_dist : float
        distance to buffer around the place geometry, in meters

    Returns
    -------
    GeoDataFrame
    """

    # ensure query type
    assert (isinstance(query, dict) or isinstance(query, str)), 'query must be a dict or a string'

    # get the data from OSM
    data = downloader._osm_polygon_download(query, limit=which_result)
    if len(data) >= which_result:

        # extract data elements from the JSON response
        result = data[which_result - 1]
        bbox_south, bbox_north, bbox_west, bbox_east = [float(x) for x in result['boundingbox']]
        geometry = result['geojson']
        place = result['display_name']
        features = [{'type': 'Feature',
                     'geometry': geometry,
                     'properties': {'place_name': place,
                                    'bbox_north': bbox_north,
                                    'bbox_south': bbox_south,
                                    'bbox_east': bbox_east,
                                    'bbox_west': bbox_west}}]

        # if we got an unexpected geometry type (like a point), log a warning
        if geometry['type'] not in ['Polygon', 'MultiPolygon']:
            utils.log(f'OSM returned a {geometry["type"]} as the geometry', level=lg.WARNING)

        # create the GeoDataFrame, name it, and set its original CRS to default_crs
        gdf = gpd.GeoDataFrame.from_features(features)
        gdf.crs = settings.default_crs

        # if buffer_dist was passed in, project the geometry to UTM, buffer it
        # in meters, then project it back to lat-long
        if buffer_dist is not None:
            gdf_utm = projection.project_gdf(gdf)
            gdf_utm['geometry'] = gdf_utm['geometry'].buffer(buffer_dist)
            gdf = projection.project_gdf(gdf_utm, to_latlong=True)
            utils.log(f'Buffered GeoDataFrame to {buffer_dist} meters')

        # return the gdf
        utils.log(f'Created GeoDataFrame with {len(gdf)} row for query "{query}"')
        return gdf
    else:
        # if no data returned (or fewer results than which_result)
        utils.log(f'OSM returned no results (or fewer than which_result) for query "{query}"', level=lg.WARNING)
        return gpd.GeoDataFrame()



def gdf_from_places(queries, which_results=None, buffer_dist=None):
    """
    Create a GeoDataFrame from a list of place names to query.

    Parameters
    ----------
    queries : list
        list of query strings or structured query dicts to geocode/download,
        one at a time
    which_results : list
        if not None, a list of max number of results to return and which to
        process upon receipt, for each query in queries
    buffer_dist : float
        distance to buffer around the place geometry, in meters

    Returns
    -------
    GeoDataFrame
    """
    # create an empty GeoDataFrame then append each result as a new row,
    # checking for the presence of which_results
    gdf = gpd.GeoDataFrame()
    if which_results is not None:
        assert len(queries) == len(which_results), 'which_results list length must be the same as queries list length'
        for query, which_result in zip(queries, which_results):
            gdf = gdf.append(gdf_from_place(query, buffer_dist=buffer_dist, which_result=which_result))
    else:
        for query in queries:
            gdf = gdf.append(gdf_from_place(query, buffer_dist=buffer_dist))

    # reset the index
    gdf = gdf.reset_index(drop=True)

    # set the original CRS of the GeoDataFrame to default_crs, and return it
    gdf.crs = settings.default_crs
    utils.log(f'Finished creating GeoDataFrame with {len(gdf)} rows from {len(queries)} queries')
    return gdf



def _osm_net_download(polygon=None, north=None, south=None, east=None, west=None,
                      network_type='all_private', timeout=180, memory=None,
                      max_query_area_size=50*1000*50*1000, infrastructure='way["highway"]',
                      custom_filter=None, custom_settings=None):
    """
    Download OSM ways and nodes within some bounding box from the Overpass API.

    Parameters
    ----------
    polygon : shapely Polygon or MultiPolygon
        geographic shape to fetch the street network within
    north : float
        northern latitude of bounding box
    south : float
        southern latitude of bounding box
    east : float
        eastern longitude of bounding box
    west : float
        western longitude of bounding box
    network_type : string
        {'walk', 'bike', 'drive', 'drive_service', 'all', 'all_private'} what
        type of street network to get
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size : float
        max area for any part of the geometry in meters: any polygon bigger
        will get divided up for multiple queries to API (default 50km x 50km)
    infrastructure : string
        download infrastructure of given type. default is streets, ie,
        'way["highway"]') but other infrastructures may be selected like power
        grids, ie, 'way["power"~"line"]'
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones

    Returns
    -------
    response_jsons : list
    """

    # check if we're querying by polygon or by bounding box based on which
    # argument(s) where passed into this function
    by_poly = polygon is not None
    by_bbox = not (north is None or south is None or east is None or west is None)
    if not (by_poly or by_bbox):
        raise InsufficientNetworkQueryArguments(
            'You must pass a polygon or north, south, east, and west')

    # create a filter to exclude certain kinds of ways based on the requested
    # network_type
    if custom_filter:
        osm_filter = custom_filter
    else:
        osm_filter = downloader._get_osm_filter(network_type)
    response_jsons = []

    # pass server memory allocation in bytes for the query to the API
    # if None, pass nothing so the server will use its default allocation size
    # otherwise, define the query's maxsize parameter value as whatever the
    # caller passed in
    if memory is None:
        maxsize = ''
    else:
        maxsize = f'[maxsize:{memory}]'

    # use custom settings if delivered, otherwise just the default ones.
    if custom_settings:
        overpass_settings = custom_settings
    else:
        overpass_settings = settings.default_overpass_query_settings.format(timeout=timeout, maxsize=maxsize)

    # define the query to send the API
    # specifying way["highway"] means that all ways returned must have a highway
    # key. the {filters} then remove ways by key/value. the '>' makes it recurse
    # so we get ways and way nodes. maxsize is in bytes.
    if by_bbox:
        # turn bbox into a polygon and project to local UTM
        polygon = Polygon([(west, south), (east, south), (east, north), (west, north)])
        geometry_proj, crs_proj = projection.project_geometry(polygon)

        # subdivide it if it exceeds the max area size (in meters), then project
        # back to lat-long
        gpcs = utils_geo._consolidate_subdivide_geometry(geometry_proj, max_query_area_size=max_query_area_size)
        geometry, _ = projection.project_geometry(gpcs, crs=crs_proj, to_latlong=True)
        utils.log(f'Requesting network data within bounding box from API in {len(geometry)} request(s)')

        # loop through each polygon rectangle in the geometry (there will only
        # be one if original bbox didn't exceed max area size)
        for poly in geometry:
            # represent bbox as south,west,north,east and round lat-longs to 6
            # decimal places (ie, ~100 mm) so URL strings aren't different
            # due to float rounding issues (for consistent caching)
            west, south, east, north = poly.bounds
            query_str = f'{overpass_settings};({infrastructure}{osm_filter}({south:.6f},{west:.6f},{north:.6f},{east:.6f});>;);out;'
            response_json = downloader.overpass_request(data={'data':query_str}, timeout=timeout)
            response_jsons.append(response_json)
        utils.log(f'Got all network data within bounding box from API in {len(geometry)} request(s)')

    elif by_poly:
        # project to utm, divide polygon up into sub-polygons if area exceeds a
        # max size (in meters), project back to lat-long, then get a list of
        # polygon(s) exterior coordinates
        geometry_proj, crs_proj = projection.project_geometry(polygon)
        gpcs = utils_geo._consolidate_subdivide_geometry(geometry_proj, max_query_area_size=max_query_area_size)
        geometry, _ = projection.project_geometry(gpcs, crs=crs_proj, to_latlong=True)
        polygon_coord_strs = utils_geo._get_polygons_coordinates(geometry)
        utils.log(f'Requesting network data within polygon from API in {len(polygon_coord_strs)} request(s)')

        # pass each polygon exterior coordinates in the list to the API, one at
        # a time
        for polygon_coord_str in polygon_coord_strs:
            query_str = f'{overpass_settings};({infrastructure}{osm_filter}(poly:"{polygon_coord_str}");>;);out;'
            response_json = downloader.overpass_request(data={'data':query_str}, timeout=timeout)
            response_jsons.append(response_json)
        utils.log(f'Got all network data within polygon from API in {len(polygon_coord_strs)} request(s)')

    return response_jsons



def _convert_node(element):
    """
    Convert an OSM node element into the format for a networkx node.

    Parameters
    ----------
    element : dict
        an OSM node element

    Returns
    -------
    dict
    """

    node = {}
    node['y'] = element['lat']
    node['x'] = element['lon']
    node['osmid'] = element['id']
    if 'tags' in element:
        for useful_tag in settings.useful_tags_node:
            if useful_tag in element['tags']:
                node[useful_tag] = element['tags'][useful_tag]
    return node



def _convert_path(element):
    """
    Convert an OSM way element into the format for a networkx graph path.

    Parameters
    ----------
    element : dict
        an OSM way element

    Returns
    -------
    dict
    """

    path = {}
    path['osmid'] = element['id']

    # remove any consecutive duplicate elements in the list of nodes
    grouped_list = groupby(element['nodes'])
    path['nodes'] = [group[0] for group in grouped_list]

    if 'tags' in element:
        for useful_tag in settings.useful_tags_path:
            if useful_tag in element['tags']:
                path[useful_tag] = element['tags'][useful_tag]
    return path



def _parse_osm_nodes_paths(osm_data):
    """
    Construct dicts of nodes and paths with key=osmid and value=dict of
    attributes.

    Parameters
    ----------
    osm_data : dict
        JSON response from from the Overpass API

    Returns
    -------
    nodes, paths : tuple
    """

    nodes = {}
    paths = {}
    for element in osm_data['elements']:
        if element['type'] == 'node':
            key = element['id']
            nodes[key] = _convert_node(element)
        elif element['type'] == 'way': #osm calls network paths 'ways'
            key = element['id']
            paths[key] = _convert_path(element)

    return nodes, paths



def _add_path(G, data, one_way):
    """
    Add a path to the graph.

    Parameters
    ----------
    G : networkx multidigraph
    data : dict
        the attributes of the path
    one_way : bool
        if this path is one-way or if it is bi-directional

    Returns
    -------
    None
    """

    # extract the ordered list of nodes from this path element, then delete it
    # so we don't add it as an attribute to the edge later
    path_nodes = data['nodes']
    del data['nodes']

    # set the oneway attribute to the passed-in value, to make it consistent
    # True/False values, but only do this if you aren't forcing all edges to
    # oneway with the all_oneway setting. With the all_oneway setting, you
    # likely still want to preserve the original OSM oneway attribute.
    if not settings.all_oneway:
        data['oneway'] = one_way

    # zip together the path nodes so you get tuples like (0,1), (1,2), (2,3)
    # and so on
    path_edges = list(zip(path_nodes[:-1], path_nodes[1:]))
    G.add_edges_from(path_edges, **data)

    # if the path is NOT one-way
    if not one_way:
        # reverse the direction of each edge and add this path going the
        # opposite direction
        path_edges_opposite_direction = [(v, u) for u, v in path_edges]
        G.add_edges_from(path_edges_opposite_direction, **data)



def _add_paths(G, paths, bidirectional=False):
    """
    Add a collection of paths to the graph.

    Parameters
    ----------
    G : networkx multidigraph
    paths : dict
        the paths from OSM
    bidirectional : bool
        if True, create bidirectional edges for one-way streets


    Returns
    -------
    None
    """

    # the list of values OSM uses in its 'oneway' tag to denote True
    # updated list of of values OSM uses based on https://www.geofabrik.de/de/data/geofabrik-osm-gis-standard-0.7.pdf
    osm_oneway_values = ['yes', 'true', '1', '-1', 'T', 'F']

    for data in paths.values():

        if settings.all_oneway is True:
            _add_path(G, data, one_way=True)
        # if this path is tagged as one-way and if it is not a walking network,
        # then we'll add the path in one direction only
        elif ('oneway' in data and data['oneway'] in osm_oneway_values) and not bidirectional:
            if data['oneway'] == '-1' or data['oneway'] == 'T':
                # paths with a one-way value of -1 or T are one-way, but in the
                # reverse direction of the nodes' order, see osm documentation
                data['nodes'] = list(reversed(data['nodes']))
            # add this path (in only one direction) to the graph
            _add_path(G, data, one_way=True)

        elif ('junction' in data and data['junction'] == 'roundabout') and not bidirectional:
            # roundabout are also oneway but not tagged as is
            _add_path(G, data, one_way=True)

        # else, this path is not tagged as one-way or it is a walking network
        # (you can walk both directions on a one-way street)
        else:
            # add this path (in both directions) to the graph and set its
            # 'oneway' attribute to False. if this is a walking network, this
            # may very well be a one-way street (as cars/bikes go), but in a
            # walking-only network it is a bi-directional edge
            _add_path(G, data, one_way=False)

    return G



def _create_graph(response_jsons, retain_all=False, bidirectional=False):
    """
    Create a networkx graph from Overpass API HTTP response objects.

    Parameters
    ----------
    response_jsons : list
        list of dicts of JSON responses from from the Overpass API
    retain_all : bool
        if True, return the entire graph even if it is not connected
    bidirectional : bool
        if True, create bidirectional edges for one-way streets

    Returns
    -------
    networkx multidigraph
    """

    utils.log('Creating networkx graph from downloaded OSM data...')

    # make sure we got data back from the server requests
    elements = []
    for response_json in response_jsons:
        elements.extend(response_json['elements'])
    if len(elements) < 1:
        raise EmptyOverpassResponse('There are no data elements in the response JSON objects')

    # create the graph as a MultiDiGraph and set its meta-attributes
    G = nx.MultiDiGraph(created_date=utils.ts(),
                        created_with=f'OSMnx {__version__}',
                        crs=settings.default_crs)

    # extract nodes and paths from the downloaded osm data
    nodes = {}
    paths = {}
    for osm_data in response_jsons:
        nodes_temp, paths_temp = _parse_osm_nodes_paths(osm_data)
        for key, value in nodes_temp.items():
            nodes[key] = value
        for key, value in paths_temp.items():
            paths[key] = value

    # add each osm node to the graph
    for node, data in nodes.items():
        G.add_node(node, **data)

    # add each osm way (aka, path) to the graph
    G = _add_paths(G, paths, bidirectional=bidirectional)

    # retain only the largest connected component, if caller did not
    # set retain_all=True
    if not retain_all:
        G = utils_graph.get_largest_component(G)

    utils.log(f'Created graph with {len(G)} nodes and {len(G.edges())} edges')

    # add length (great circle distance between nodes) attribute to each edge to
    # use as weight
    if len(G.edges) > 0:
        G = utils_geo.add_edge_lengths(G)

    return G



def graph_from_bbox(north, south, east, west, network_type='all_private',
                    simplify=True, retain_all=False, truncate_by_edge=False,
                    timeout=180, memory=None,
                    max_query_area_size=50*1000*50*1000, clean_periphery=True,
                    infrastructure='way["highway"]', custom_filter=None,
                    custom_settings=None):
    """
    Create a networkx graph from OSM data within some bounding box.

    Parameters
    ----------
    north : float
        northern latitude of bounding box
    south : float
        southern latitude of bounding box
    east : float
        eastern longitude of bounding box
    west : float
        western longitude of bounding box
    network_type : string
        what type of street network to get
    simplify : bool
        if true, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected
    truncate_by_edge : bool
        if True retain node if it's outside bbox but at least one of node's
        neighbors are within bbox
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size : float
        max area for any part of the geometry in meters: any polygon bigger
        will get divided up for multiple queries to API (default 50km x 50km)
    clean_periphery : bool
        if True (and simplify=True), buffer 0.5km to get a graph larger than
        requested, then simplify, then truncate it to requested spatial extent
    infrastructure : string
        download infrastructure of given type (default is streets (ie, 'way["highway"]') but other
        infrastructures may be selected like power grids (ie, 'way["power"~"line"]'))
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones

    Returns
    -------
    networkx multidigraph
    """

    if clean_periphery and simplify:
        # create a new buffered bbox 0.5km around the desired one
        buffer_dist = 500
        polygon = Polygon([(west, north), (west, south), (east, south), (east, north)])
        polygon_utm, crs_utm = projection.project_geometry(geometry=polygon)
        polygon_proj_buff = polygon_utm.buffer(buffer_dist)
        polygon_buff, _ = projection.project_geometry(geometry=polygon_proj_buff, crs=crs_utm, to_latlong=True)
        west_buffered, south_buffered, east_buffered, north_buffered = polygon_buff.bounds

        # get the network data from OSM then create the graph
        response_jsons = _osm_net_download(north=north_buffered, south=south_buffered,
                                           east=east_buffered, west=west_buffered,
                                           network_type=network_type, timeout=timeout,
                                           memory=memory, max_query_area_size=max_query_area_size,
                                           infrastructure=infrastructure, custom_filter=custom_filter,
                                           custom_settings=custom_settings)
        G_buffered = _create_graph(response_jsons,
                                   retain_all=retain_all,
                                   bidirectional=network_type in settings.bidirectional_network_types)
        G = utils_geo.truncate_graph_bbox(G_buffered, north, south, east, west, retain_all=True, truncate_by_edge=truncate_by_edge)

        # simplify the graph topology
        G_buffered = simplification.simplify_graph(G_buffered)

        # truncate graph by desired bbox to return the graph within the bbox
        # caller wants
        G = utils_geo.truncate_graph_bbox(G_buffered, north, south, east, west, retain_all=retain_all, truncate_by_edge=truncate_by_edge)

        # count how many street segments in buffered graph emanate from each
        # intersection in un-buffered graph, to retain true counts for each
        # intersection, even if some of its neighbors are outside the bbox
        G.graph['streets_per_node'] = utils_graph.count_streets_per_node(G_buffered, nodes=G.nodes())

    else:
        # get the network data from OSM
        response_jsons = _osm_net_download(north=north, south=south, east=east,
                                           west=west, network_type=network_type,
                                           timeout=timeout, memory=memory,
                                           max_query_area_size=max_query_area_size,
                                           infrastructure=infrastructure, custom_filter=custom_filter,
                                           custom_settings=custom_settings)

        # create the graph, then truncate to the bounding box
        G = _create_graph(response_jsons,
                          retain_all=retain_all,
                          bidirectional=network_type in settings.bidirectional_network_types)
        G = utils_geo.truncate_graph_bbox(G, north, south, east, west, retain_all=retain_all, truncate_by_edge=truncate_by_edge)

        # simplify the graph topology as the last step. don't truncate after
        # simplifying or you may have simplified out to an endpoint
        # beyond the truncation distance, in which case you will then strip out
        # your entire edge
        if simplify:
            G = simplification.simplify_graph(G)

    utils.log(f'graph_from_bbox returned graph with {len(G)} nodes and {len(G.edges())} edges')
    return  G



def graph_from_point(center_point, distance=1000, distance_type='bbox',
                     network_type='all_private', simplify=True, retain_all=False,
                     truncate_by_edge=False, timeout=180,
                     memory=None, max_query_area_size=50*1000*50*1000,
                     clean_periphery=True, infrastructure='way["highway"]',
                     custom_filter=None, custom_settings=None):
    """
    Create a networkx graph from OSM data within some distance of some (lat,
    lon) center point.

    Parameters
    ----------
    center_point : tuple
        the (lat, lon) central point around which to construct the graph
    distance : int
        retain only those nodes within this many meters of the center of the
        graph, with distance determined according to distance_type argument
    distance_type : string
        {'network', 'bbox'} if 'bbox', retain only those nodes within a bounding
        box of the distance parameter. if 'network', retain only those nodes
        within some network distance from the center-most node.
    network_type : string
        what type of street network to get
    simplify : bool
        if true, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected
    truncate_by_edge : bool
        if True retain node if it's outside bbox but at least one of node's
        neighbors are within bbox
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size : float
        max area for any part of the geometry in meters: any polygon bigger
        will get divided up for multiple queries to API (default 50km x 50km)
    clean_periphery : bool,
        if True (and simplify=True), buffer 0.5km to get a graph larger than
        requested, then simplify, then truncate it to requested spatial extent
    infrastructure : string
        download infrastructure of given type (default is streets (ie, 'way["highway"]') but other
        infrastructures may be selected like power grids (ie, 'way["power"~"line"]'))
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones

    Returns
    -------
    networkx multidigraph
    """

    if distance_type not in ['bbox', 'network']:
        raise InvalidDistanceType('distance_type must be "bbox" or "network"')

    # create a bounding box from the center point and the distance in each
    # direction
    north, south, east, west = utils_geo.bbox_from_point(center_point, distance)

    # create a graph from the bounding box
    G = graph_from_bbox(north, south, east, west, network_type=network_type, simplify=simplify,
                        retain_all=retain_all, truncate_by_edge=truncate_by_edge,
                        timeout=timeout, memory=memory, max_query_area_size=max_query_area_size,
                        clean_periphery=clean_periphery, infrastructure=infrastructure,
                        custom_filter=custom_filter, custom_settings=custom_settings)

    # if the network distance_type is network, find the node in the graph
    # nearest to the center point, and truncate the graph by network distance
    # from this node
    if distance_type == 'network':
        centermost_node = utils_geo.get_nearest_node(G, center_point)
        G = utils_geo.truncate_graph_dist(G, centermost_node, max_distance=distance)

    utils.log(f'graph_from_point returned graph with {len(G)} nodes and {len(G.edges())} edges')
    return G



def graph_from_address(address, distance=1000, distance_type='bbox',
                       network_type='all_private', simplify=True, retain_all=False,
                       truncate_by_edge=False, return_coords=False,
                       timeout=180, memory=None,
                       max_query_area_size=50*1000*50*1000,
                       clean_periphery=True, infrastructure='way["highway"]',
                       custom_filter=None, custom_settings=None):
    """
    Create a networkx graph from OSM data within some distance of some address.

    Parameters
    ----------
    address : string
        the address to geocode and use as the central point around which to
        construct the graph
    distance : int
        retain only those nodes within this many meters of the center of the
        graph
    distance_type : string
        {'network', 'bbox'} if 'bbox', retain only those nodes within a bounding
        box of the distance parameter.
        if 'network', retain only those nodes within some network distance from
        the center-most node.
    network_type : string
        what type of street network to get
    simplify : bool
        if true, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected
    truncate_by_edge : bool
        if True retain node if it's outside bbox but at least one of node's
        neighbors are within bbox
    return_coords : bool
        optionally also return the geocoded coordinates of the address
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size
        float, max size for any part of the geometry, in square degrees: any
        polygon bigger will get divided up for multiple queries to API
    clean_periphery : bool,
        if True (and simplify=True), buffer 0.5km to get a graph larger than
        requested, then simplify, then truncate it to requested spatial extent
    infrastructure : string
        download infrastructure of given type (default is streets (ie, 'way["highway"]') but other
        infrastructures may be selected like power grids (ie, 'way["power"~"line"]'))
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones

    Returns
    -------
    networkx multidigraph or tuple
        multidigraph or optionally (multidigraph, tuple)
    """

    # geocode the address string to a (lat, lon) point
    point = utils_geo.geocode(query=address)

    # then create a graph from this point
    G = graph_from_point(point, distance, distance_type, network_type=network_type,
                         simplify=simplify, retain_all=retain_all, truncate_by_edge=truncate_by_edge,
                         timeout=timeout, memory=memory,
                         max_query_area_size=max_query_area_size,
                         clean_periphery=clean_periphery, infrastructure=infrastructure,
                         custom_filter=custom_filter, custom_settings=custom_settings)
    utils.log(f'graph_from_address returned graph with {len(G)} nodes and {len(G.edges())} edges')

    if return_coords:
        return G, point
    else:
        return G



def graph_from_polygon(polygon, network_type='all_private', simplify=True,
                       retain_all=False, truncate_by_edge=False,
                       timeout=180, memory=None,
                       max_query_area_size=50*1000*50*1000,
                       clean_periphery=True, infrastructure='way["highway"]',
                       custom_filter=None, custom_settings=None):
    """
    Create a networkx graph from OSM data within the spatial boundaries of the
    passed-in shapely polygon.

    Parameters
    ----------
    polygon : shapely Polygon or MultiPolygon
        the shape to get network data within. coordinates should be in units of
        latitude-longitude degrees.
    network_type : string
        what type of street network to get
    simplify : bool
        if true, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected
    truncate_by_edge : bool
        if True retain node if it's outside bbox but at least one of node's
        neighbors are within bbox
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size : float
        max area for any part of the geometry in meters: any polygon bigger
        will get divided up for multiple queries to API (default 50km x 50km)
    clean_periphery : bool
        if True (and simplify=True), buffer 0.5km to get a graph larger than
        requested, then simplify, then truncate it to requested spatial extent
    infrastructure : string
        download infrastructure of given type (default is streets
        (ie, 'way["highway"]') but other infrastructures may be selected
        like power grids (ie, 'way["power"~"line"]'))
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones

    Returns
    -------
    networkx multidigraph
    """

    # verify that the geometry is valid and is a shapely Polygon/MultiPolygon
    # before proceeding
    if not polygon.is_valid:
        raise TypeError('Shape does not have a valid geometry')
    if not isinstance(polygon, (Polygon, MultiPolygon)):
        raise TypeError('Geometry must be a shapely Polygon or MultiPolygon. If you requested '
                         'graph from place name or address, make sure your query resolves to a '
                         'Polygon or MultiPolygon, and not some other geometry, like a Point. '
                         'See OSMnx documentation for details.')

    if clean_periphery and simplify:
        # create a new buffered polygon 0.5km around the desired one
        buffer_dist = 500
        polygon_utm, crs_utm = projection.project_geometry(geometry=polygon)
        polygon_proj_buff = polygon_utm.buffer(buffer_dist)
        polygon_buffered, _ = projection.project_geometry(geometry=polygon_proj_buff, crs=crs_utm, to_latlong=True)

        # get the network data from OSM,  create the buffered graph, then
        # truncate it to the buffered polygon
        response_jsons = _osm_net_download(polygon=polygon_buffered, network_type=network_type,
                                           timeout=timeout, memory=memory,
                                           max_query_area_size=max_query_area_size,
                                           infrastructure=infrastructure, custom_filter=custom_filter,
                                           custom_settings=custom_settings)
        G_buffered = _create_graph(response_jsons,
                                   retain_all=True,
                                   bidirectional=network_type in settings.bidirectional_network_types)
        G_buffered = utils_geo.truncate_graph_polygon(G_buffered, polygon_buffered, retain_all=True, truncate_by_edge=truncate_by_edge)

        # simplify the graph topology
        G_buffered = simplification.simplify_graph(G_buffered)

        # truncate graph by polygon to return the graph within the polygon that
        # caller wants. don't simplify again - this allows us to retain
        # intersections along the street that may now only connect 2 street
        # segments in the network, but in reality also connect to an
        # intersection just outside the polygon
        G = utils_geo.truncate_graph_polygon(G_buffered, polygon, retain_all=retain_all, truncate_by_edge=truncate_by_edge)

        # count how many street segments in buffered graph emanate from each
        # intersection in un-buffered graph, to retain true counts for each
        # intersection, even if some of its neighbors are outside the polygon
        G.graph['streets_per_node'] = utils_graph.count_streets_per_node(G_buffered, nodes=G.nodes())

    else:
        # download a list of API responses for the polygon/multipolygon
        response_jsons = _osm_net_download(polygon=polygon, network_type=network_type,
                                           timeout=timeout, memory=memory,
                                           max_query_area_size=max_query_area_size,
                                           infrastructure=infrastructure, custom_filter=custom_filter,
                                           custom_settings=custom_settings)

        # create the graph from the downloaded data
        G = _create_graph(response_jsons,
                          retain_all=True,
                          bidirectional=network_type in settings.bidirectional_network_types)

        # truncate the graph to the extent of the polygon
        G = utils_geo.truncate_graph_polygon(G, polygon, retain_all=retain_all, truncate_by_edge=truncate_by_edge)

        # simplify the graph topology as the last step. don't truncate after
        # simplifying or you may have simplified out to an endpoint beyond the
        # truncation distance, in which case you will then strip out your entire
        # edge
        if simplify:
            G = simplification.simplify_graph(G)

    utils.log(f'graph_from_polygon returned graph with {len(G)} nodes and {len(G.edges())} edges')
    return G



def graph_from_place(query, network_type='all_private', simplify=True,
                     retain_all=False, truncate_by_edge=False,
                     which_result=1, buffer_dist=None, timeout=180, memory=None,
                     max_query_area_size=50*1000*50*1000, clean_periphery=True,
                     infrastructure='way["highway"]', custom_filter=None,
                     custom_settings=None):
    """
    Create a networkx graph from OSM data within the spatial boundaries of some
    geocodable place(s).

    The query must be geocodable and OSM must have polygon boundaries for the
    geocode result. If OSM does not have a polygon for this place, you can
    instead get its street network using the graph_from_address function, which
    geocodes the place name to a point and gets the network within some distance
    of that point. Alternatively, you might try to vary the which_result
    parameter to use a different geocode result. For example, the first geocode
    result (ie, the default) might resolve to a point geometry, but the second
    geocode result for this query might resolve to a polygon, in which case you
    can use graph_from_place with which_result=2.

    Parameters
    ----------
    query : string or dict or list
        the place(s) to geocode/download data for
    network_type : string
        what type of street network to get
    simplify : bool
        if true, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected
    truncate_by_edge : bool
        if True retain node if it's outside bbox but at least one of node's
        neighbors are within bbox
    which_result : int
        max number of results to return and which to process upon receipt
    buffer_dist : float
        distance to buffer around the place geometry, in meters
    timeout : int
        the timeout interval for requests and to pass to API
    memory : int
        server memory allocation size for the query, in bytes. If none, server
        will use its default allocation size
    max_query_area_size : float
        max area for any part of the geometry in meters: any polygon bigger
        will get divided up for multiple queries to API (default 50km x 50km)
    clean_periphery : bool
        if True (and simplify=True), buffer 0.5km to get a graph larger than
        requested, then simplify, then truncate it to requested spatial extent
    infrastructure : string
        download infrastructure of given type (default is streets (ie, 'way["highway"]') but other
        infrastructures may be selected like power grids (ie, 'way["power"~"line"]'))
    custom_filter : string
        a custom network filter to be used instead of the network_type presets
    custom_settings : string
        custom settings to be used in the overpass query instead of the default
        ones
    Returns
    -------
    networkx multidigraph
    """

    # create a GeoDataFrame with the spatial boundaries of the place(s)
    if isinstance(query, str) or isinstance(query, dict):
        # if it is a string (place name) or dict (structured place query), then
        # it is a single place
        gdf_place = gdf_from_place(query, which_result=which_result, buffer_dist=buffer_dist)
    elif isinstance(query, list):
        # if it is a list, it contains multiple places to get
        gdf_place = gdf_from_places(query, buffer_dist=buffer_dist)
    else:
        raise TypeError('query must be a string or a list of query strings')

    # extract the geometry from the GeoDataFrame to use in API query
    polygon = gdf_place['geometry'].unary_union
    utils.log('Constructed place geometry polygon(s) to query API')

    # create graph using this polygon(s) geometry
    G = graph_from_polygon(polygon, network_type=network_type, simplify=simplify,
                           retain_all=retain_all, truncate_by_edge=truncate_by_edge,
                           timeout=timeout, memory=memory,
                           max_query_area_size=max_query_area_size,
                           clean_periphery=clean_periphery, infrastructure=infrastructure,
                           custom_filter=custom_filter, custom_settings=custom_settings)

    utils.log(f'graph_from_place returned graph with {len(G)} nodes and {len(G.edges())} edges')
    return G



def graph_from_file(filename, bidirectional=False, simplify=True,
                    retain_all=False):
    """
    Create a networkx graph from OSM data in an XML file.

    Parameters
    ----------
    filename : string
        the name of a file containing OSM XML data
    bidirectional : bool
        if True, create bidirectional edges for one-way streets
    simplify : bool
        if True, simplify the graph topology
    retain_all : bool
        if True, return the entire graph even if it is not connected

    Returns
    -------
    networkx multidigraph
    """
    # transmogrify file of OSM XML data into JSON
    response_jsons = [_overpass_json_from_file(filename)]

    # create graph using this response JSON
    G = _create_graph(response_jsons, bidirectional=bidirectional,
                      retain_all=retain_all)

    # simplify the graph topology as the last step.
    if simplify:
        G = simplification.simplify_graph(G)

    utils.log(f'graph_from_file returned graph with {len(G)} nodes and {len(G.edges())} edges')
    return G



def _overpass_json_from_file(filename):
    """
    Read OSM XML from input filename and return Overpass-like JSON.

    Parameters
    ----------
    filename : string
        name of file containing OSM XML data

    Returns
    -------
    OSMContentHandler object
    """

    _, ext = os.path.splitext(filename)

    if ext == '.bz2':
        # Use Python 2/3 compatible BZ2File()
        opener = lambda fn: bz2.BZ2File(fn)
    else:
        # Assume an unrecognized file extension is just XML
        opener = lambda fn: open(fn, mode='rb')

    with opener(filename) as file:
        handler = _OSMContentHandler()
        xml.sax.parse(file, handler)
        return handler.object



class _OSMContentHandler(xml.sax.handler.ContentHandler):
    """
    SAX content handler for OSM XML.

    Used to build an Overpass-like response JSON object in self.object. For format
    notes, see http://wiki.openstreetmap.org/wiki/OSM_XML#OSM_XML_file_format_notes
    and http://overpass-api.de/output_formats.html#json
    """

    def __init__(self):
        self._element = None
        self.object = {'elements': []}

    def startElement(self, name, attrs):
        if name == 'osm':
            self.object.update({k: attrs[k] for k in attrs.keys()
                                if k in ('version', 'generator')})

        elif name in ('node', 'way'):
            self._element = dict(type=name, tags={}, nodes=[], **attrs)
            self._element.update({k: float(attrs[k]) for k in attrs.keys()
                                  if k in ('lat', 'lon')})
            self._element.update({k: int(attrs[k]) for k in attrs.keys()
                                  if k in ('id', 'uid', 'version', 'changeset')})

        elif name == 'tag':
            self._element['tags'].update({attrs['k']: attrs['v']})

        elif name == 'nd':
            self._element['nodes'].append(int(attrs['ref']))

        elif name == 'relation':
            # Placeholder for future relation support.
            # Look for nested members and tags.
            pass

    def endElement(self, name):
        if name in ('node', 'way'):
            self.object['elements'].append(self._element)
