# -*- coding: utf-8 -*-
"""
/***************************************************************************
 DsgTools
                                 A QGIS plugin
 Brazilian Army Cartographic Production Tools
                              -------------------
        begin                : 2018-11-13
        git sha              : $Format:%H$
        copyright            : (C) 2018 by João P. Esperidião - Cartographic Engineer @ Brazilian Army
        email                : esperidiao.joao@eb.mil.br
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

from qgis.PyQt.QtCore import QObject
from qgis.core import QgsFeatureRequest, QgsProject

from DsgTools.core.dsgEnums import DsgEnums
from DsgTools.core.Factories.DbFactory.dbFactory import DbFactory
from DsgTools.core.Factories.LayerLoaderFactory.layerLoaderFactory import LayerLoaderFactory
from DsgTools.core.GeometricTools.layerHandler import LayerHandler

class DbConverter(QObject):
    """
    Class designed to manipulate the map generated by the Datasource Conversion tool.
    What it should be doing:
    1- read map;
    2- get layers ready;
        * in this step, layers are just supposed to be read, no filters applied, in order to be reused, if needed.
    3- prepare each conversion as 1 separately;
        3.a- apply filters for each layer - layer level;
        3.b- apply feature map to destination - feature level; and
    4- each successfully filtered and mapped layer will be then sent to be perpetuated to output - layer level.
    """
    def __init__(self, iface, conversionMap=None):
        """
        Class constructor.
        :param iface: (QgsInterface) QGIS interface object (for runtime operations).
        :param conversionMap: (dict) conversion map generated by Datasource Conversion tool.
        """
        super(DbConverter, self).__init__()
        self.iface = iface
        self.conversionMap = conversionMap

    def getConversionCount(self, conversionMap=None):
        """
        Gets how many conversion procedures are required.
        :param conversionMap: (dict) conversion map generated by Datasource Conversion tool.
        :return: (int) number of conversion cycles.
        """
        if conversionMap is None:
            # to allow this module to be instantiated without a map prepared
            conversionMap = self.conversionMap
        count = 0
        for outMaps in conversionMap.values():
            count += len(outMaps)
        return count

    def getAllUniqueInputDb(self, conversionMap=None):
        """
        Get a list of all UNIQUE input datasources.
        :param conversionMap: (dict) conversion map generated by Datasource Conversion tool.
        :return: (list-of-str) list of all input connections necessary.
        """
        if conversionMap is None:
            # to allow this module to be instantiated without a map prepared
            conversionMap = self.conversionMap
        dsList = []
        for ds in conversionMap:
            # datasources are key to conversion map dict
            if ds not in dsList:
                dsList.append(ds)
        return dsList

    def getAllUniqueOutputDb(self, conversionMap=None):
        """
        Get a list of all UNIQUE output datasources.
        :param conversionMap: (dict) conversion map generated by Datasource Conversion tool.
        :return: (list-of-str) list of all output connections necessary.
        """
        if conversionMap is None:
            # to allow this module to be instantiated without a map prepared
            conversionMap = self.conversionMap
        dsList = []
        for ds, convMaps in conversionMap.items():
            # datasources are key to conversion map dict
            for convMap in convMaps:
                ds = convMap['outDs']
                if ds not in dsList:
                    dsList.append(ds)
        return dsList

    def getPgParamaters(self, parameters, conn):
        """
        Retrieves Postgres connection parameters from its connection string.
        :param parameters: (dict) parameter dict to have data saved at.
        :param conn: (str) connection string.
        """
        # connection string: USER@HOST:PORT.DATABASE
        parameters['username'], part = conn.split('@')
        parameters['host'], part = part.split(':')
        parameters['port'], parameters['db'] = part.split('.')

    def parseDatasourcePath(self, datasource):
        """
        Reads and identifies datasource's driver and separates it into connection parameters.
        :param datasouce: (str) datasource path string.
        :return: (dict) a dict containing all connection parameters.
        """
        drivers = {
            'pg' : DsgEnums.DriverPostGIS,
            'sqlite' : DsgEnums.DriverSpatiaLite,
            'shp' : DsgEnums.DriverShapefile,
            'gpkg' : DsgEnums.DriverGeopackage
            }
        parameters = dict()
        driver = datasource.split(':')[0]
        conn = datasource[len(driver) + 1:]
        if driver == 'pg':
            self.getPgParamaters(parameters=parameters, conn=conn)
        else:
            parameters['path'] = conn
        parameters['driver'] = drivers[driver]
        return parameters

    def connectToPostgis(self, parameters):
        """
        Stablishes connection to a Postgis database.
        :param parameters: (dict) a dict containing all connection parameters.
        :return: (AbstractDb) returns the DSGTools database object.
        """
        user, host, port, db = parameters['username'], parameters['host'], parameters['port'], parameters['db']
        # initiate abstractDb
        abstractDb = DbFactory().createDbFactory(driver=DsgEnums.DriverPostGIS)
        # ignore all info except for the password
        _, _, _, password = abstractDb.getServerConfiguration(name=host)
        return abstractDb if abstractDb.testCredentials(host, port, db, user, password) else None

    def connectToSpatialite(self, parameters):
        """
        Stablishes connection to a SpatiaLite database.
        :param parameters: (dict) a dict containing all connection parameters.
        :return: (AbstractDb) returns the DSGTools database object.
        """
        abstractDb = DbFactory().createDbFactory(driver=DsgEnums.DriverSpatiaLite)
        abstractDb.connectDatabase(conn=parameters['path'])
        return abstractDb if abstractDb.getDatabaseName() else None

    def connectToGeopackage(self, parameters):
        """
        Stablishes connection to a Geopackage database.
        :param parameters: (dict) a dict containing all connection parameters.
        :return: (AbstractDb) returns the DSGTools database object.
        """
        abstractDb = DbFactory().createDbFactory(driver=DsgEnums.DriverGeopackage)
        abstractDb.connectDatabase(conn=parameters['path'])
        return abstractDb if abstractDb.getDatabaseName() else None

    def connectToShapefile(self, parameters):
        """
        Stablishes connection to a Shapefile dataset.
        :param parameters: (dict) a dict containing all connection parameters.
        :return: (AbstractDb) returns the DSGTools database object.
        """
        abstractDb = DbFactory().createDbFactory(driver=DsgEnums.DriverShapefile)
        abstractDb.connectDatabase(conn=parameters['path'])
        return abstractDb if abstractDb.getDatabaseName() else None

    def connectToDb(self, parameters):
        """
        Stablishes a connection to the datasource described by a set of connection parameters.
        :param parameters: (dict) a dict containing all connection parameters.
        :return: (AbstractDb) returns the DSGTools database object.
        """
        drivers = {
            DsgEnums.DriverPostGIS : lambda : self.connectToPostgis(parameters=parameters),
            DsgEnums.DriverSpatiaLite : lambda : self.connectToSpatialite(parameters=parameters),
            DsgEnums.DriverGeopackage : lambda : self.connectToGeopackage(parameters=parameters),
            DsgEnums.DriverShapefile : lambda : self.connectToShapefile(parameters=parameters)
        }
        driver = parameters['driver']
        return drivers[driver]() if driver in drivers else None

    def readInputLayers(self, conversionMap=None):
        """
        Reads all input datasources and return its layers.
        :param conversionMap: (dict) conversion map generated by Datasource Conversion tool.
        :return: (dict) returns a map of ds to its layers.
        """
        # STILL TO DECIDE WHAT TO DO IN CASE OF READING ERRORS (ignore? raise alert at the end? raise exception?)
        if conversionMap is None:
            # to allow this module to be instantiated without a map prepared
            conversionMap = self.conversionMap
        inputList = self.getAllUniqueInputDb(conversionMap=conversionMap)
        inputLayerMap = dict()
        failedInputs = []
        for ds in inputList:
            parameters = self.parseDatasourcePath(ds)
            # read datasource abstract
            abstractDb = self.connectToDb(parameters=parameters)
            if abstractDb is None:
                # if connection wasn't successfull, add it to failed list and skip cycle 
                failedInputs.append(ds)
                continue
            # read layers
            layerLoader = LayerLoaderFactory().makeLoader(self.iface, abstractDb)
            layers = abstractDb.listClassesWithElementsFromDatabase(useComplex=False).keys()
            inputLayerMap[ds] = {l : layerLoader.getLayerByName(l) for l in layers}
            complexLayers = abstractDb.listComplexClassesFromDatabase()
            complexMap = {l : layerLoader.getComplexLayerByName(l) for l in complexLayers}
            inputLayerMap[ds].update(complexMap)
        return inputLayerMap

    def applySpatialFilters(self, layers, spatialFilter, fanOut):
        """
        Applies the spatial filter to given layers.
        :param layers: (list-of-QgsVectorLayer) layers to be spatially filtered.
        :param spatialFilter: (dict) spatial filtering options from a conversion map.
        :param fanOut: (bool) indicates whether a fanOut will be applied in case of spatial filtering.
        :return: (list) list of layers (list-of-QgsVectorLayer) after the spatial filter.
        """
        out = dict()
        # move all spatial operation to handlers (layer, feature, etc)
        referenceLayerName = spatialFilter['layer_name']
        # spatial filter is only applicable if a layer was chosen as reference to topological tests
        if referenceLayerName != "":
            # get a layer handler for spatial predicate operation
            lh = LayerHandler()
            referenceLayer = QgsProject.instance().mapLayersByName(referenceLayerName)[0]
            # if spatialFilter['layer_filter']:
            #     req = QgsFeatureRequest().setFilterExpression(spatialFilter['layer_filter'])
            #     features = referenceLayer.getFeatures(req)
            # else:
            #     features = referenceLayer.getFeatures()
            req = QgsFeatureRequest().setFilterExpression(spatialFilter['layer_filter']) if spatialFilter['layer_filter'] == '' else None
            predicate = spatialFilter["filter_type"]
            parameter = spatialFilter["topological_relation"]
            applySpatialFilter = lambda layer : lh.spatialFilter(referenceLayer, layer, predicate, parameter, req, fanOut)
            for ln, vl in layers.items():
                for ref_if, featList in applySpatialFilter(vl).items():
                    if vl.name() == referenceLayerName:
                        continue
                    if ref_if not in out:
                        out[ref_if] = dict()
                    if ln not in out[ref_if]:
                        out[ref_if][ln] = []
                    out[ref_if][ln] += featList
            # if fanOut:
            #     for feature in features:
            #         # for each feature requested from reference layer, an output dataset is expected
            #         feat_id = feature.id()
            #         out[feat_id] = dict()
            #         # create a memory layer with a single feature, set 
            #         for ln, vl in layers.items():
            #             out[feat_id][ln] = lh.spatialFilter(feature, vl, predicate, parameter, fanOut)
            # else:
            #     # for all feature requested from reference layer, ONE output dataset is expected
            #     out[0] = dict()
            #     for feature in features:
            #         for ln, vl in layers:
            #             if ln not in out[0]:
            #                 out[0][ln] = []
                        # out[0][ln] += lh.spatialFilter(feature, vl, predicate, parameter)
        return out

    def prepareLayers(self, layers, filters, fanOut):
        """
        Prepare layers for each translation unit (step) to be executed (e.g. applies filters).
        :param layers: (dict) layers (list-of-QgsVectorLayer) to be filtered.
        :param filters: (dict) filtering option from a conversion map.
        :param fanOut: (bool) indicates whether a fanOut will be applied in case of spatial filtering.
        :return: (list) a list of dict, mapping the layers to be translated to their filtered features.
        """
        # initiate output
        outFeatureMap = dict()
        # apply layer selection filter
        if filters['layer']:
            # in case a selection of layers was executed, only selected layers should pass
            filteredLayers = dict()
            for l in filters['layer']:
                filteredLayers[l] = layers[l]
        else:
            # in case no selection was made, all layers should be translated
            filteredLayers = layers
        # apply spatial filters
        outFeatureMap = self.applySpatialFilters(layers=filteredLayers,\
                            spatialFilter=filters['spatial_filter'], fanOut=fanOut)
        # for l, vl in filteredLayers.items():
        #     # add all features from non-filtered layers (by expression)
        #     if l in filters['layer_filter']:
        #         # apply the filtering expression, if provided
        #         req = QgsFeatureRequest().setFilterExpression(exp)
        #         outFeatureMap[l] = [f for f in vl.getFeatures(req)]
        #         # ignore the ones that were filtered by expression
        #     else:
        #         outFeatureMap[l] = [f for f in vl.getFeatures()]
        return outFeatureMap