# -*- coding: utf-8 -*-
"""
/***************************************************************************
 DsgTools
                                 A QGIS plugin
 Brazilian Army Cartographic Production Tools
                              -------------------
        begin                : 2018-05-01
        git sha              : $Format:%H$
        copyright            : (C) 2018 by Philipe Borba - Cartographic Engineer @ Brazilian Army
        email                : borba.philipe@eb.mil.br
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
from __future__ import absolute_import
from builtins import range
from qgis.core import QgsMessageLog, QgsVectorLayer, QgsGeometry, QgsField, QgsVectorDataProvider, \
                      QgsFeatureRequest, QgsExpression, QgsFeature, QgsSpatialIndex, Qgis, QgsCoordinateTransform, QgsWkbTypes
from qgis.PyQt.Qt import QObject

from .geometryHandler import GeometryHandler
from .attributeHandler import AttributeHandler

class FeatureHandler(QObject):
    def __init__(self, iface = None, parent = None):
        super(FeatureHandler, self).__init__()
        self.parent = parent
        self.iface = iface
        if iface:
            self.canvas = iface.mapCanvas()
        self.geometryHandler = GeometryHandler(iface)
        self.attributeHandler = AttributeHandler(iface)
    
    def reclassifyFeatures(self, featureList, destinationLayer, reclassificationDict, coordinateTransformer, parameterDict):
        newFeatList = []
        deleteList = []
        for feat in featureList:
            geom = self.geometryHandler.reprojectWithCoordinateTransformer(feat.geometry(), coordinateTransformer)
            geomList = self.geometryHandler.adjustGeometry(geom, parameterDict)
            newFeatList += self.createFeaturesWithAttributeDict(geomList, feat, reclassificationDict, destinationLayer)
            deleteList.append(feat.id())
        return newFeatList, deleteList
    
    def createFeaturesWithAttributeDict(self, geomList, originalFeat, attributeDict, destinationLayer):
        """
        Creates a newFeatureList using each geom from geomList. attributeDict is used to set attributes
        """
        newFeatureList = []
        fields = destinationLayer.fields()
        for geom in geomList:
            newFeature = QgsFeature(fields)
            newFeature.setGeometry(geom)
            newFeature = self.attributeHandler.setFeatureAttributes(newFeature, attributeDict, oldFeat = originalFeat)
            newFeatureList.append(newFeature)
        return newFeatureList
    
    def populateUnifiedLayer(self, unifiedLyr, layerList, attributeTupple = False, attributeBlackList = '', onlySelected = False):
        featList = []
        for layer in layerList:
            # recording class name
            classname = layer.name()
            iterator, total = self.getIteratorAndFeatureCount(layer, onlySelected=onlySelected)
            for feature in iterator:
                newFeat = self.createUnifiedFeature(unifiedLyr, feature, classname, bList=)
                featlist.append(newfeat)
        return featList

    def createUnifiedFeature(self, unifiedLyr, feature, classname, bList = [], attributeTupple = False):
        newfeat = QgsFeature(unifiedLyr.fields())
        newfeat.setGeometry(feature.geometry())
        newfeat['featid'] = feature.id()
        newfeat['classname'] = classname
        if attributeTupple:
            newfeat['tupple'] = self.attributeHandler.getTuppleAttribute(feature, unifiedLyr, bList=bList)
        return newfeat
    
    