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

from collections import defaultdict
import copy
from functools import partial
from itertools import combinations
import os

from processing.tools import dataobjects

import concurrent.futures

from DsgTools.core.DSGToolsProcessingAlgs.algRunner import AlgRunner
from DsgTools.core.Utils.FrameTools.map_index import UtmGrid
from qgis.analysis import QgsGeometrySnapper, QgsInternalGeometrySnapper
from qgis.core import (edit, Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsExpression, QgsFeature, QgsFeatureRequest, QgsField, QgsFields, QgsGeometry, QgsMessageLog,
                       QgsProcessingContext, QgsProcessingMultiStepFeedback, QgsProcessingUtils, QgsProject,
                       QgsSpatialIndex, QgsVectorDataProvider, QgsVectorLayer, QgsVectorLayerUtils, QgsWkbTypes,
                       QgsProcessingFeatureSourceDefinition, QgsFeatureSink)
from qgis.PyQt.Qt import QObject, QVariant

from .featureHandler import FeatureHandler
from .geometryHandler import GeometryHandler


class LayerHandler(QObject):
    def __init__(self, iface=None, parent=None):
        super(LayerHandler, self).__init__()
        self.parent = parent
        self.iface = iface
        if iface:
            self.canvas = iface.mapCanvas()
        self.featureHandler = FeatureHandler(iface)
        self.geometryHandler = GeometryHandler(iface)
        self.algRunner = AlgRunner()

    def getFeatureList(self, lyr, onlySelected=False, returnIterator=True, returnSize=True):
        """
        Gets the features from lyr acording to parameters.
        :param (QgsVectorLayer) lyr: layer;
        :param (bool) onlySelected: if true, only fetches selected features from layer;
        :param (bool) returnIterator: if true, returns the iterator object;
        :param (bool) returnSize: if true, return the featureList and size.
        """
        if onlySelected:
            featureList = lyr.getSelectedFeatures() if returnIterator else [
                i for i in lyr.getSelectedFeatures()]
            size = lyr.selectedFeatureCount()
        else:
            featureList = lyr.getFeatures() if returnIterator else [
                i for i in lyr.getFeatures()]
            size = lyr.featureCount()
        if returnSize:
            return featureList, size
        else:
            return featureList

    def getSelectedFeatures(self, lyr):
        return lyr.selectedFeatures()

    def getSelectedFeaturesFromCanvasLayers(self):
        """
        looks on iface and returns a dict where key is lyr and value is a list of features
        """
        selectedDict = dict()
        for lyr in self.canvas.layers():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            featureList = self.getFeatureList(
                lyr, onlySelected=True, returnIterator=False)
            if featureList:
                selectedDict[lyr] = featureList
        return selectedDict

    def reclassifySelectedFeatures(self, destinationLayer, reclassificationDict):
        """
        Gets a destination layer and uses reclassificationDict to reclassify each selected feature
        """
        selectedDict = self.getSelectedFeaturesFromCanvasLayers()
        parameterDict = self.getDestinationParameters(destinationLayer)
        reclassifyCount = 0
        destinationLayer.startEditing()
        destinationLayer.beginEditCommand(self.tr('DsgTools reclassification'))
        for lyr, featureList in [(k, v) for k, v in selectedDict.items() if len(v[0]) > 0]:
            featureList = featureList[0] if isinstance(
                featureList, tuple) else featureList
            coordinateTransformer = self.getCoordinateTransformer(
                lyr, destinationLayer)
            newFeatList, deleteList = self.featureHandler.reclassifyFeatures(
                featureList, lyr, reclassificationDict, coordinateTransformer, parameterDict)
            featuresAdded = destinationLayer.addFeatures(newFeatList)
            if featuresAdded:
                lyr.startEditing()
                lyr.deleteFeatures(deleteList)
                reclassifyCount += len(deleteList)
        return reclassifyCount

    def getDestinationParameters(self, destinationLayer):
        """
        Gets the layer parameters such as geometryType (geomType), if it
        has m-values (hasMValues), if it has z-values (hasZValues) and
        if it is a multi-geometry (isMulti)
        """
        parameterDict = dict()
        parameterDict['geomType'] = destinationLayer.geometryType()
        # generic check (not every database is implemented as ours)
        parameterDict['hasMValues'] = QgsWkbTypes.hasM(
            int(destinationLayer.wkbType()))
        parameterDict['hasZValues'] = QgsWkbTypes.hasZ(
            int(destinationLayer.wkbType()))    #
        parameterDict['isMulti'] = QgsWkbTypes.isMultiType(
            int(destinationLayer.wkbType()))
        return parameterDict

    def getCoordinateTransformer(self, inputLyr, outputLyr):
        """
        If inputLyr and outputLyr have different crs, creates a Coordinate Transform
        and returns it. Otherwise, returns None.
        """
        inputAuthId = inputLyr.crs().authid()
        outputAuthId = outputLyr.crs().authid()
        if inputAuthId == outputAuthId:
            return None
        inputSrc = QgsCoordinateReferenceSystem(inputAuthId)
        outputSrc = QgsCoordinateReferenceSystem(outputAuthId)
        coordinateTransformer = QgsCoordinateTransform(
            inputSrc, outputSrc, QgsProject.instance())
        return coordinateTransformer

    def createAndPopulateUnifiedVectorLayer(self, layerList, geomType=None, epsg=None, attributeTupple=False, attributeBlackList='', onlySelected=False, feedback=None):
        if not epsg:
            epsg = layerList[0].crs().authid().split(':')[-1]
        if not geomType:
            geomType = None
            for layer in layerList:
                if layer.featureCount() > 0:
                    geomType = int(next(layer.getFeatures())\
                        .geometry().wkbType())
                    break
            else:
                raise ValueError(
                    self.tr("No layers were provided or they are all empty."))
        unified_layer = self.createUnifiedVectorLayer(geomType, epsg,
                                                      attributeTupple=attributeTupple)
        parameterDict = self.getDestinationParameters(unified_layer)
        featList = self.getUnifiedLayerFeatures(unified_layer, layerList,
                                                attributeTupple=attributeTupple,
                                                attributeBlackList=attributeBlackList,
                                                onlySelected=onlySelected,
                                                parameterDict=parameterDict,
                                                feedback=feedback)
        self.addFeaturesToLayer(unified_layer, featList,
                                msg='Populating unified layer')
        return unified_layer

    def createUnifiedVectorLayer(self, geomType, srid, attributeTupple=False):
        """
        Creates a unified vector layer for validation purposes.
        """
        fields = self.getUnifiedVectorFields(attributeTupple=attributeTupple)
        lyrUri = "{0}?crs=epsg:{1}".format(
            QgsWkbTypes.displayString(int(geomType)), srid)
        lyr = QgsVectorLayer(lyrUri, "unified_layer", "memory")
        lyr.startEditing()
        fields = self.getUnifiedVectorFields(attributeTupple=attributeTupple)
        lyr.dataProvider().addAttributes(fields)
        lyr.updateFields()
        return lyr

    def getUnifiedVectorFields(self, attributeTupple=False):
        if not attributeTupple:
            fields = [QgsField('featid', QVariant.Int),
                      QgsField('layer', QVariant.String)
                      ]
        else:
            fields = [QgsField('featid', QVariant.Int),
                      QgsField('layer', QVariant.String),
                      QgsField('tupple', QVariant.String),
                      QgsField('blacklist', QVariant.String)
                      ]
        return fields

    def getUnifiedLayerFeatures(self, unifiedLyr, layerList, attributeTupple=False, attributeBlackList=None, onlySelected=False, parameterDict=None, feedback=None):
        parameterDict = {} if parameterDict is None else parameterDict
        featList = []
        blackList = attributeBlackList.split(
            ',') if attributeBlackList is not None and ',' in attributeBlackList else []
        if feedback:
            multiStepFeedback = QgsProcessingMultiStepFeedback(
                len(layerList), feedback)
        for i, layer in enumerate(layerList):
            if feedback:
                if feedback.isCanceled():
                    break
                multiStepFeedback.setCurrentStep(i)
            # recording class name
            layername = layer.name()
            coordinateTransformer = self.getCoordinateTransformer(
                unifiedLyr, layer)
            iterator, size = self.getFeatureList(
                layer, onlySelected=onlySelected, returnSize=True)
            for current, feature in enumerate(iterator):
                if feedback:
                    if multiStepFeedback.isCanceled():
                        break
                newFeats = self.featureHandler.createUnifiedFeature(unifiedLyr, feature, layername,
                                                                    bList=blackList,
                                                                    attributeTupple=attributeTupple,
                                                                    parameterDict=parameterDict,
                                                                    coordinateTransformer=coordinateTransformer)
                featList += newFeats
                if feedback:
                    multiStepFeedback.setProgress(current*size)
        return featList

    def addFeaturesToLayer(self, lyr, featList, commitChanges=True, msg=None):
        msg = '' if msg is None else msg
        lyr.startEditing()
        lyr.beginEditCommand(msg)
        res = lyr.addFeatures(featList)
        lyr.endEditCommand()
        if commitChanges:
            lyr.commitChanges()
        return res

    def splitUnifiedLayer(self, unifiedLyr, lyrList):
        """
        Updates layers from lyrList with features from unifiedLyr
        """

        for lyr in lyrList:
            self.updateOriginalLayerFromUnifiedLayer(lyr, unifiedLyr)

    def buildInputDict(self, inputLyr, pk=None, feedback=None, onlySelected=False):
        """
        Maps inputLyr into a dict with its attributes.
        """
        inputDict = dict()
        if onlySelected:
            iterator = inputLyr.getSelectedFeatures()
            if feedback is not None:
                localTotal = 100/inputLyr.selectedFeatureCount() if inputLyr.selectedFeatureCount() else 0
        else:
            request = QgsFeatureRequest()
            iterator = inputLyr.getFeatures(request)
            if feedback is not None:
                localTotal = 100/inputLyr.featureCount() if inputLyr.featureCount() else 0
        for current, feature in enumerate(iterator):
            if feedback and feedback.isCanceled():
                break
            key = feature[pk] if pk else feature.id()
            inputDict[key] = {
                'featList': [],
                'originalFeat': feature,
            }
            if feedback is not None:
                feedback.setProgress(localTotal*current)
        return inputDict

    def populateInputDictFeatList(self, lyr, inputDict, pk=None, request=None, feedback=None):
        iterator = lyr.getFeatures(request) if request else lyr.getFeatures()
        localTotal = 100/lyr.featureCount() if lyr.featureCount() else 0
        for current, feat in enumerate(iterator):
            if feedback:
                if feedback.isCanceled():
                    break
            fid = feat[pk] if pk else feat.id()
            if fid in inputDict:
                inputDict[fid]['featList'].append(feat)
            if feedback:
                feedback.setProgress(localTotal*current)

    def updateOriginalLayer(self, originalLayer, resultLayer, field=None, feedback=None, keepFeatures=False, onlySelected=True):
        multiStepFeedback = QgsProcessingMultiStepFeedback(
            3, feedback) if feedback else None
        # 1- build inputDict structure to store the original state of the layer
        if feedback:
            multiStepFeedback.setCurrentStep(0)
        inputDict = self.buildInputDict(
            originalLayer, pk=field, feedback=multiStepFeedback, onlySelected=onlySelected)
        # 2- populate the inputDict with the features from the resultLayer
        if feedback:
            multiStepFeedback.setCurrentStep(1)
        self.populateInputDictFeatList(
            resultLayer, inputDict, pk=field, feedback=multiStepFeedback)
        # 3- get information from originalLayer and resultLayer
        if feedback:
            multiStepFeedback.setCurrentStep(2)
        parameterDict = self.getDestinationParameters(originalLayer)
        coordinateTransformer = self.getCoordinateTransformer(
            resultLayer, originalLayer)
        # 4- run update original layer
        self.updateOriginalLayerFeatures(originalLayer, inputDict, parameterDict=parameterDict,
                                         coordinateTransformer=coordinateTransformer, keepFeatures=keepFeatures, feedback=multiStepFeedback)

    def updateOriginalLayersFromUnifiedLayer(self, lyrList, unifiedLyr, feedback=None, onlySelected=False):
        """
        Updates original layers from the unified layers
        """
        lenList = len(lyrList)
        parameterDict = self.getDestinationParameters(unifiedLyr)
        multiStepFeedback = QgsProcessingMultiStepFeedback(
            3*lenList, feedback) if feedback else None
        for i, lyr in enumerate(lyrList):
            if feedback is not None and multiStepFeedback.isCanceled():
                break
            multiStepFeedback.setCurrentStep(3*i)
            multiStepFeedback.pushInfo(self.tr(f"Building {lyr.name()} input dict"))
            inputDict = self.buildInputDict(
                lyr, onlySelected=onlySelected, feedback=multiStepFeedback)
            if multiStepFeedback is not None and multiStepFeedback.isCanceled():
                break
            request = QgsFeatureRequest(QgsExpression(
                "layer = '{0}'".format(lyr.name())))
            multiStepFeedback.setCurrentStep(3*i+1)
            multiStepFeedback.pushInfo(self.tr(f"Populating {lyr.name()} input dict"))
            self.populateInputDictFeatList(
                unifiedLyr, inputDict, pk='featid', request=request, feedback=multiStepFeedback)
            if multiStepFeedback is not None and multiStepFeedback.isCanceled():
                break
            coordinateTransformer = self.getCoordinateTransformer(
                unifiedLyr, lyr)
            multiStepFeedback.setCurrentStep(3*i+2)
            multiStepFeedback.pushInfo(self.tr(f"Updating {lyr.name()} features"))
            self.updateOriginalLayerFeatures(lyr, inputDict, parameterDict=parameterDict,
                                             coordinateTransformer=coordinateTransformer, feedback=multiStepFeedback)

    def updateOriginalLayerFeatures(self, lyr, inputDict,
                                    parameterDict=None,
                                    coordinateTransformer=None,
                                    keepFeatures=False,
                                    feedback=None):
        """
        Updates the layer list using the given inputDict
        :param lyr: (QgsVectorLayer) layer container of features from featList.
        :param inputDict: (dict) a with featList and originalFeat.
        :param parameterDict: (dict) a dict with features parameters.
        :param coordinateTransformer: (QgsCoordinateTransform) a coordinate
            transformer.
        :param keepFeatures: (bool) a boolean that indicates to keep or not
            feature that must be deleted.
        :param feedback: (QgsProcessingMultiStepFeedback) feedback.
        """
        parameterDict = {} if parameterDict is None else parameterDict
        idsToRemove, featuresToAdd = set(), set()
        lyr.startEditing()
        lyr.beginEditCommand('Updating layer {0}'.format(lyr.name()))
        localTotal = 100/len(inputDict) if inputDict else 0
        futures = set()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()-1)
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
        def evaluate(id_, featDict):
            idsToRemove, featuresToAdd, geometriesToChange = set(), set(), set()
            if multiStepFeedback.isCanceled():
                return idsToRemove, featuresToAdd, geometriesToChange
            outFeats = featDict['featList']
            if len(outFeats) == 0:
                idsToRemove.add(id_)
                return idsToRemove, featuresToAdd, geometriesToChange
            geomToUpdate, addedFeatures, deleteId = self.featureHandler.handleFeature(
                outFeats,
                featDict['originalFeat'],
                lyr,
                parameterDict=parameterDict,
                coordinateTransformer=coordinateTransformer
            )
            if geomToUpdate is not None and not geomToUpdate.equals(inputDict[id_]['originalFeat'].geometry()):
                geometriesToChange.add((id_, geomToUpdate))
            if deleteId:
                idsToRemove.add(id_)
            featuresToAdd = set(addedFeatures)
            return idsToRemove, featuresToAdd, geometriesToChange

        multiStepFeedback.setCurrentStep(0)
        multiStepFeedback.pushInfo(self.tr("Submitting tasks to thread..."))
        for current, (id_, featDict) in enumerate(inputDict.items()):
            if feedback is not None and feedback.isCanceled():
                return
            futures.add(pool.submit(evaluate, id_, featDict))
            if feedback is not None:
                feedback.setProgress(localTotal*current)
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.pushInfo(self.tr("Evaluating results..."))
        changeGeometryLambda = lambda x: lyr.changeGeometry(x[0], x[1], skipDefaultValue=True)
        for current, future in enumerate(concurrent.futures.as_completed(futures)):
            if feedback is not None and feedback.isCanceled():
                return
            deletedIds, addedFeatures, geometriesToChange = future.result()
            list(map(changeGeometryLambda, geometriesToChange))
            featuresToAdd = featuresToAdd.union(addedFeatures)
            idsToRemove = idsToRemove.union(deletedIds)
            if feedback is not None:
                feedback.setProgress(localTotal*current)
        lyr.addFeatures(list(featuresToAdd))
        if not keepFeatures:
            lyr.deleteFeatures(list(idsToRemove))
        lyr.endEditCommand()

    def mergeLinesOnLayer(self, lyr, onlySelected=False, feedback=None, ignoreVirtualFields=True, attributeBlackList=None, excludePrimaryKeys=True):
        attributeBlackList = [] if attributeBlackList is None else attributeBlackList
        if feedback:
            localFeedback = QgsProcessingMultiStepFeedback(3, feedback)
            localFeedback.setCurrentStep(0)
        # build attribute dict
        attributeFeatDict = self.buildAttributeFeatureDict(
            lyr, onlySelected=onlySelected, feedback=localFeedback, attributeBlackList=attributeBlackList)

        # build network dict
        if feedback:
            localFeedback.setCurrentStep(1)
        networkDict = self.buildInitialAndEndPointDict(
            lyr, onlySelected=onlySelected, feedback=localFeedback)

        parameterDict = self.getDestinationParameters(lyr)

        idsToRemove = []
        if feedback:
            localFeedback.setCurrentStep(2)
            localTotal = 100/(len(attributeFeatDict)
                              ) if len(attributeFeatDict) != 0 else 0
            mergeFeedback = QgsProcessingMultiStepFeedback(
                len(attributeFeatDict), localFeedback)
        lyr.startEditing()
        lyr.beginEditCommand(self.tr('Merging Lines'))

        for current, (key, featList) in enumerate(attributeFeatDict.items()):
            if feedback:
                if feedback.isCanceled():
                    break
                mergeFeedback.setCurrentStep(current)
            self.featureHandler.mergeLineFeatures(
                featList=featList,
                lyr=lyr,
                idsToRemove=idsToRemove,
                parameterDict=parameterDict,
                feedback=mergeFeedback,
                networkDict=networkDict
            )
        lyr.deleteFeatures(idsToRemove)
        lyr.endEditCommand()

    def buildAttributeFeatureDict(self, lyr, onlySelected=False, feedback=None, ignoreVirtualFields=True, attributeBlackList=None, excludePrimaryKeys=True):
        iterator, size = self.getFeatureList(lyr, onlySelected=onlySelected)
        localTotal = 100/size if size != 0 else 0
        attributeFeatDict = dict()
        columns = self.getAttributesFromBlackList(
            lyr, attributeBlackList=attributeBlackList, ignoreVirtualFields=ignoreVirtualFields, excludePrimaryKeys=excludePrimaryKeys)
        for current, feat in enumerate(iterator):
            if feedback:
                if feedback.isCanceled():
                    break
            self.appendFeatOnAttrsDict(attributeFeatDict, feat, columns)
            if feedback:
                feedback.setProgress(localTotal*current)
        return attributeFeatDict

    def getAttributesFromBlackList(self, lyr, attributeBlackList=None, ignoreVirtualFields=True, excludePrimaryKeys=True):
        attributeBlackList = [] if attributeBlackList is None else attributeBlackList
        pkIndexes = lyr.primaryKeyAttributes() if excludePrimaryKeys else []
        typeBlackList = [6] if ignoreVirtualFields else []
        columns = [field.name() for idx, field in enumerate(lyr.fields()) if idx not in pkIndexes and field.type(
        ) not in typeBlackList and field.name() not in attributeBlackList]
        return columns

    def appendFeatOnAttrsDict(self, inputDict, feat, columns):
        attrKey = ','.join(['{}'.format(feat[column]) for column in columns])
        if attrKey not in inputDict:
            inputDict[attrKey] = []
        inputDict[attrKey].append(feat)

    def buildInitialAndEndPointDict(self, lyr, onlySelected=False, feedback=None, addFeatureToList=False, recordStepProgress=True):
        """
        Calculates initial point and end point from each line from lyr.
        """
        # start and end points dict
        endVerticesDict = dict()
        # iterating over features to store start and end points
        iterator = lyr.getFeatures() if not onlySelected else lyr.getSelectedFeatures()
        if recordStepProgress:
            featCount = lyr.featureCount() if not onlySelected else lyr.selectedFeatureCount()
            if featCount == 0:
                return endVerticesDict
            size = 100 / featCount
        for current, feat in enumerate(iterator):
            if feedback is not None and feedback.isCanceled():
                break
            geom = feat.geometry()
            lineList = geom.asMultiPolyline() if geom.isMultipart() else [
                geom.asPolyline()]
            for line in lineList:
                self.addFeatToDict(
                    endVerticesDict=endVerticesDict,
                    line=line,
                    item=feat if addFeatureToList else feat.id()
                )
            if feedback is not None and recordStepProgress:
                feedback.setProgress(size*current)
        return endVerticesDict

    def getDuplicatedFeaturesDict(self, lyr, onlySelected=False, attributeBlackList=None, ignoreVirtualFields=True, excludePrimaryKeys=True, useAttributes=False, feedback=None):
        """
        returns geomDict = {
            'bbox_geom' : {geomKey : -list of duplicated feats-}
        }
        """
        geomDict = dict()
        isMulti = QgsWkbTypes.isMultiType(int(lyr.wkbType()))
        iterator, featCount = self.getFeatureList(
            lyr, onlySelected=onlySelected, returnIterator=True)
        size = 100/featCount if featCount else 0
        columns = self.getAttributesFromBlackList(
            lyr, attributeBlackList=attributeBlackList, ignoreVirtualFields=ignoreVirtualFields, excludePrimaryKeys=excludePrimaryKeys)
        multiStepFeedback = QgsProcessingMultiStepFeedback(
            2, feedback) if feedback else None
        multiStepFeedback.setCurrentStep(0)
        # builds bounding box dict to do a geos comparison for each feat in list
        bbDict = self.getFeaturesWithSameBoundingBox(
            iterator, isMulti, featCount, columns=columns, feedback=feedback)
        multiStepFeedback.setCurrentStep(1)
        for current, (key, featList) in enumerate(bbDict.items()):
            if feedback is not None and feedback.isCanceled():
                break
            if len(featList) > 1:
                duplicatedDict = self.searchDuplicatedFeatures(
                    featList, columns=columns, useAttributes=useAttributes)
                geomDict.update(duplicatedDict)
            if feedback is not None:
                feedback.setProgress(size * current)
        return geomDict

    def getFeaturesWithSameBoundingBox(self, iterator, isMulti, size, columns=None, feedback=None):
        """
        Iterates over iterator and gets 
        """
        bbDict = defaultdict(list)
        if feedback is not None:
            feedback.setProgressText(self.tr("Building duplicated search structure..."))
        def _buildBBDictEntry(feat, columns):
            if feedback.isCanceled():
                return
            geom = feat.geometry()
            if isMulti and not geom.isMultipart():
                geom.convertToMultiType()
            geomBB_key = geom.boundingBox().asWktPolygon()
            attrKey = ','.join(['{}'.format(feat[column])
                                for column in columns]) if columns is not None else ''
            return (geomBB_key, {'geom': geom, 'feat': feat, 'attrKey': attrKey})
        futures = set()
        pool = concurrent.futures.ThreadPoolExecutor(os.cpu_count()-1)
        func = lambda x: _buildBBDictEntry(x, columns)
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback) if feedback is not None else None
        multiStepFeedback.setCurrentStep(0)
        multiStepFeedback.pushInfo(self.tr("Submitting tasks to thread"))
        for feat in iterator:
            if feedback is not None and feedback.isCanceled():
                break
            futures.add(pool.submit(func, QgsFeature(feat)))
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.pushInfo(self.tr("Evaluating results"))
        for current, x in enumerate(concurrent.futures.as_completed(futures)):
            if multiStepFeedback is not None and multiStepFeedback.isCanceled():
                break
            key, value = x.result()
            bbDict[key].append(value)
            if multiStepFeedback is not None:
                multiStepFeedback.setProgress(size * current)
        return bbDict
        # """
        # Iterates over iterator and gets 
        # """
        # bbDict = defaultdict(list)
        # for current, feat in enumerate(iterator):
        #     if feedback is not None and feedback.isCanceled():
        #         break
        #     geom = feat.geometry()
        #     if isMulti and not geom.isMultipart():
        #         geom.convertToMultiType()
        #     geomKey = geom.asWkb()
        #     geomBB_key = geom.boundingBox().asWktPolygon()
        #     attrKey = ','.join(['{}'.format(feat[column])
        #                         for column in columns]) if columns is not None else ''
        #     bbDict[geomBB_key].append(
        #         {'geom': geom, 'feat': feat, 'attrKey': attrKey})
        #     if feedback is not None:
        #         feedback.setProgress(size * current)
        # return bbDict

    def searchDuplicatedFeatures(self, featList, columns, useAttributes=False):
        """
        featList = list of {'geom': geom, 'feat':feat}
        returns {geomKey : -list of duplicated feats-}
        """
        duplicatedDict = dict()
        if featList:
            fields = [f.name() for f in featList[0]['feat'].fields()]
        for dict_feat1, dict_feat2 in combinations(featList, 2):
            geom1 = dict_feat1['geom']
            geom2 = dict_feat2['geom']
            feat1 = dict_feat1['feat']
            feat2 = dict_feat2['feat']
            wkb1 = geom1.asWkb()
            wkb2 = geom2.asWkb()
            if not geom1.isGeosEqual(geom2):
                continue
            elif useAttributes:
                # do things to check attributes
                try:
                    for attr in fields:
                        if attr not in columns:
                            continue
                        elif feat1[attr] != feat2[attr]:
                            raise Exception('Skip outter loop')
                except:
                    continue
            if wkb1 in duplicatedDict:
                duplicatedDict[wkb1].append(feat2)
            elif wkb2 in duplicatedDict:
                duplicatedDict[wkb2].append(feat1)
            else:
                duplicatedDict[wkb1] = [feat1, feat2]
        return duplicatedDict

    def addFeatToDict(self, endVerticesDict, line, item):
        self.addPointToDict(line[0], endVerticesDict, item)
        self.addPointToDict(line[len(line) - 1], endVerticesDict, item)

    def addPointToDict(self, point, pointDict, item):
        if point not in pointDict:
            pointDict[point] = []
        pointDict[point].append(item)

    def addDissolveField(self, layer, tol, feedback=None):
        # add temp field
        idField = QgsField('d_id', QVariant.Int)
        layer.dataProvider().addAttributes([idField])
        layer.updateFields()
        # small feature list

        if feedback:
            multiStepFeedback = QgsProcessingMultiStepFeedback(3, feedback)
            multiStepFeedback.setCurrentStep(0)
        else:
            multiStepFeedback = None
        smallFeatureList, bigFeatureList, bigFeatIndex = self.buildSizeSearchStructure(
            layer, tol, feedback=multiStepFeedback)

        if multiStepFeedback:
            multiStepFeedback.setCurrentStep(1)
        self.populateSizeSearchStructure(
            layer, smallFeatureList, bigFeatIndex, feedback=multiStepFeedback)

        if multiStepFeedback:
            multiStepFeedback.setCurrentStep(2)
        updateDict = self.updateFeaturesWithSize(
            layer, smallFeatureList, bigFeatureList, feedback=multiStepFeedback)
        return layer

    def getCandidates(self, idx, bbox):
        return idx.intersects(bbox)

    def buildSizeSearchStructure(self, layer, tol, feedback=None):
        """
        Builds search structure according to layer and tol.

        Returns a list with small features, another list with big features and the spatial index of the big feats.
        """
        smallFeatureList = []
        bigFeatureList = []
        bigFeatIndex = QgsSpatialIndex()

        featSize = layer.featureCount()
        size = 100 / featSize if featSize else 0

        for current, feat in enumerate(layer.getFeatures()):
            if feedback:
                if feedback.isCanceled():
                    break
            feat['d_id'] = feat['featid']
            if feat.geometry().area() < float(tol):
                smallFeatureList.append(feat)
            else:
                bigFeatIndex.addFeature(feat)
                bigFeatureList.append(feat)
            if feedback:
                feedback.setProgress(size * current)
        return smallFeatureList, bigFeatureList, bigFeatIndex

    def populateSizeSearchStructure(self, layer, smallFeatureList, bigFeatIndex, feedback=None):
        # using spatial index to speed up the process
        featSize = len(smallFeatureList)
        size = 100 / featSize if featSize else 0
        for current, sfeat in enumerate(smallFeatureList):
            if feedback:
                if feedback.isCanceled():
                    break
            candidates = bigFeatIndex.intersects(
                sfeat.geometry().boundingBox())
            for candidate in candidates:
                bfeat = [i for i in layer.dataProvider().getFeatures(
                    QgsFeatureRequest(candidate))][0]
                if sfeat['d_id'] == sfeat['featid'] and sfeat.geometry().intersects(bfeat.geometry()) and sfeat['tupple'] == bfeat['tupple']:
                    sfeat['d_id'] = bfeat['featid']
            if feedback:
                feedback.setProgress(size * current)

    def updateFeaturesWithSize(self, layer, smallFeatureList, bigFeatureList, feedback=None):
        updateDict = dict()
        idx = layer.fieldNameIndex('tupple')
        featList = smallFeatureList + bigFeatureList
        featSize = len(featList)
        size = 100 / featSize if featSize else 0
        for current, feat in enumerate(featList):
            if feedback:
                if feedback.isCanceled():
                    break
            newValue = u'{0},{1}'.format(feat['tupple'], feat['d_id'])
            updateDict[feat.id()] = {idx: newValue}
            if feedback:
                feedback.setProgress(size * current)
        return updateDict
    
    def searchDanglesOnPointDict(self, endVerticesDict, feedback):
        """
        Counts the number of points on each endVerticesDict's key and returns a list of QgsPoint built from key candidate.
        """
        pointList = []
        nVertexes = len(endVerticesDict)
        localTotal = 100/nVertexes if nVertexes else 0
        # actual search for dangles
        for current, point in enumerate(endVerticesDict):
            if feedback.isCanceled():
                break
            # this means we only have one occurrence of point, therefore it is a dangle
            if len(endVerticesDict[point]) <= 1:
                pointList.append(point)
            feedback.setProgress(localTotal*current)
        return pointList
    
    def getSmallFirstOrderDanglesFromPointDict(self, endVerticesDict, minLength, feedback=None):
        pointList = []
        nVertexes = len(endVerticesDict)
        localTotal = 100/nVertexes if nVertexes else 0
        for current, (point, featList) in enumerate(endVerticesDict.items()):
            if feedback is not None and feedback.isCanceled():
                break
            if len(featList) == 1 and featList[0].geometry().length() < minLength:
                pointList.append(featList[0])
            if feedback is not None:
                feedback.setProgress(current * localTotal)
        return pointList
    
    def getSmallFirstOrderDangles(self, inputLyr, minLength, onlySelected=False, feedback=None):
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
        multiStepFeedback.setProgressText(self.tr("Building aux structure"))
        multiStepFeedback.setCurrentStep(0)
        endVerticesDict = self.buildInitialAndEndPointDict(
            inputLyr,
            onlySelected=onlySelected,
            feedback=multiStepFeedback,
            addFeatureToList=True
        )
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.setProgressText(self.tr("Searching small first order dangles"))
        pointList = self.getSmallFirstOrderDanglesFromPointDict(
            endVerticesDict=endVerticesDict, minLength=minLength, feedback=multiStepFeedback
        )
        return pointList

    def filterDangles(self, lyr, searchRadius, feedback=None):
        deleteSet = set()
        if feedback is not None:
            multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
            multiStepFeedback.setCurrentStep(0)
        else:
            multiStepFeedback = None
        spatialIdx, idDict = self.buildSpatialIndexAndIdDict(
            lyr, feedback=multiStepFeedback)
        if multiStepFeedback is not None:
            multiStepFeedback.setCurrentStep(1)
        featSize = len(idDict)
        size = 100 / featSize if featSize else 0
        for current, (id, feat) in enumerate(idDict.items()):
            if feedback is not None and feedback.isCanceled():
                break
            if id not in deleteSet:
                buffer = feat.geometry().buffer(searchRadius, -1)
                bufferBB = buffer.boundingBox()
                # gets candidates from spatial index
                candidateIds = spatialIdx.intersects(bufferBB)
                for fid in candidateIds:
                    if fid != id and fid not in deleteSet and buffer.intersects(feat.geometry()):
                        deleteSet.add(fid)
            if feedback is not None:
                multiStepFeedback.setProgress(size * current)

        lyr.startEditing()
        lyr.beginEditCommand('Filter dangles')
        lyr.deleteFeatures(list(deleteSet))
        lyr.commitChanges()

    def buildSpatialIndexAndIdDict(self, inputLyr, feedback=None, featureRequest=None):
        """
        creates a spatial index for the input layer
        :param inputLyr: (QgsVectorLayer) input layer;
        :param feedback: (QgsProcessingFeedback) processing feedback;
        :param featureRequest: (QgsFeatureRequest) optional feature request;
        """
        spatialIdx = QgsSpatialIndex()
        idDict = {}
        featCount = inputLyr.featureCount()
        size = 100/featCount if featCount else 0
        iterator = inputLyr.getFeatures(
        ) if featureRequest is None else inputLyr.getFeatures(featureRequest)

        def addFeatureAlias(x): return self.addFeatureToSpatialIndex(
            current=x[0],
            feat=x[1],
            spatialIdx=spatialIdx,
            idDict=idDict,
            size=size,
            feedback=feedback
        )
        list(map(addFeatureAlias, enumerate(iterator)))
        return spatialIdx, idDict

    def addFeatureToSpatialIndex(self, current, feat, spatialIdx, idDict, size, feedback):
        """
        Adds feature to spatial index. Used along side with a python map operator
        to improve performance.
        :param current : (int) current index
        :param feat : (QgsFeature) feature to be added on spatial index and on idDict
        :param spatialIdx: (QgsSpatialIndex) spatial index
        :param idDict: (dict) dictionary with format {feat.id(): feat}
        :param size: (int) size to be used to update feedback
        :param feedback: (QgsProcessingFeedback) feedback to be used on processing
        """
        if feedback is not None and feedback.isCanceled():
            return
        idDict[feat.id()] = feat
        spatialIdx.addFeature(feat)
        if feedback is not None:
            feedback.setProgress(size * current)

    def getFrameOutterBounds(self, frameLayer, algRunner, context, feedback=None):
        """
        Gets the outter bounds of all frame features composing frame layer.
        :param frameLayer: (QgsVectorLayer) frame layer.
        :return: (list-of-QgsGeometry) list of all disjuncts outter bounds of features in frame layer.
        """
        frameGeomList = []
        if feedback is not None:
            multiStepFeedback = QgsProcessingMultiStepFeedback(3, feedback)
            multiStepFeedback.setCurrentStep(0)
        else:
            multiStepFeedback = None
        # dissolve every feature into a single one
        outputLayer = algRunner.runDissolve(
            frameLayer, context, feedback=multiStepFeedback)
        if feedback is not None:
            multiStepFeedback.setCurrentStep(1)
        boundaryLayer = algRunner.runBoundary(
            outputLayer, context, feedback=multiStepFeedback)
        # get all frame outter layer found
        if feedback is not None:
            multiStepFeedback.setCurrentStep(2)
            featCount = boundaryLayer.featureCount()
            size = 100/featCount if featCount else 0
        for current, feat in enumerate(boundaryLayer.getFeatures()):
            if feedback is not None and feedback.isCanceled():
                break
            geom = feat.geometry()
            # deaggregate geometry, if necessary
            frameGeomList += self.geometryHandler.deaggregateGeometry(
                multiGeom=geom)
            if feedback is not None:
                multiStepFeedback.setProgress(size * current)
        return frameGeomList

    def identifyAllNodes(self, networkLayer):
        """
        Identifies all nodes from a given layer (or selected features of it). The result is returned as a dict of dict.
        :param networkLayer: target layer to which nodes identification is required.
        :return: { node_id : { start : [feature_which_starts_with_node], end : feature_which_ends_with_node } }.
        """
        nodeDict = dict()
        isMulti = QgsWkbTypes.isMultiType(int(networkLayer.wkbType()))
        if self.parameters['Only Selected']:
            features = networkLayer.selectedFeatures()
        else:
            features = [feat for feat in networkLayer.getFeatures()]
        for feat in features:
            nodes = self.DsgGeometryHandler.getFeatureNodes(networkLayer, feat)
            if nodes:
                if isMulti:
                    if len(nodes) > 1:
                        # if feat is multipart and has more than one part, a flag should be raised
                        continue  # CHANGE TO RAISE FLAG
                    elif len(nodes) == 0:
                        # if no part is found, skip feature
                        continue
                    else:
                        # if feat is multipart, "nodes" is a list of list
                        nodes = nodes[0]
                # initial node
                pInit, pEnd = nodes[0], nodes[-1]
                # filling starting node information into dictionary
                if pInit not in nodeDict:
                    # if the point is not already started into dictionary, it creates a new item
                    nodeDict[pInit] = {'start': [], 'end': []}
                if feat not in nodeDict[pInit]['start']:
                    nodeDict[pInit]['start'].append(feat)
                # filling ending node information into dictionary
                if pEnd not in nodeDict:
                    nodeDict[pEnd] = {'start': [], 'end': []}
                if feat not in nodeDict[pEnd]['end']:
                    nodeDict[pEnd]['end'].append(feat)
        return nodeDict

    def snapToLayer(self, inputLyr, refLyr, tol, behavior, onlySelected=False, feedback=None):
        """
        Snaps and updates inpytLyr
        """
        snapper = QgsGeometrySnapper(
            refLyr) if inputLyr != refLyr and behavior != 7 else QgsInternalGeometrySnapper(tol, behavior)
        iterator, featCount = self.getFeatureList(
            inputLyr, onlySelected=onlySelected)
        if featCount == 0:
            return
        size = 100/featCount
        deleteSet = set()
        inputLyr.startEditing()
        inputLyr.beginEditCommand('Snapping Features')
        def evaluate(feat):
            if feedback is not None and feedback.isCanceled():
                return None
            featid = feat.id()
            geom = feat.geometry()
            if not feat.hasGeometry() or geom.isNull() or geom.isEmpty():
                return featid
            if geom.type() == QgsWkbTypes.LineGeometry and geom.length() < tol:
                return featid
            geom.removeDuplicateNodes()
            fixedGeom = geom.makeValid()
            if fixedGeom.isNull():
                return None
            outputGeom = snapper.snapGeometry(fixedGeom, tol, behavior) \
                if inputLyr != refLyr and behavior != 7 else snapper.snapFeature(feat)
            if geom is None:
                return featid
            return featid, outputGeom
        futures = set()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()-1)
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
        multiStepFeedback.setCurrentStep(0)
        for current, feat in enumerate(iterator):
            if multiStepFeedback.isCanceled():
                break
            futures.add(pool.submit(evaluate, feat))
            multiStepFeedback.setProgress(current * size)
        multiStepFeedback.setCurrentStep(1)
        for current, future in enumerate(concurrent.futures.as_completed(futures)):
            if multiStepFeedback.isCanceled():
                break
            result = future.result()
            if result is None:
                continue
            if isinstance(result, int):
                deleteSet.add(result)
                continue
            featid, outputGeom = result
            inputLyr.changeGeometry(featid, outputGeom)
            if multiStepFeedback is not None:
                multiStepFeedback.setProgress(size * current)
        inputLyr.deleteFeatures(list(deleteSet))
        inputLyr.endEditCommand()

    def getContourLineOutOfThreshold(self, contourLyr, terrainPolygonLyr, threshold, refLyr=None, feedback=None):
        """
        todo
        """
        # 1. Build contour spatial index

        # 2.
        pass

    def filterByExpression(self, layer, expression, context, feedback=None):
        """
        Filters a given layer using a filtering expression. The original layer is not modified.
        :param layer: (QgsVectorLayer) layer to be filtered.
        :param expression: (str) expression to be used as filter.
        :param context: (QgsProcessingContext) processing context in which algorithm should be executed.
        :param feedback: (QgsFeedback) QGIS feedback component (progress bar).
        :return: (QgsVectorLayer) filtered layer.
        """
        return AlgRunner().runFilterExpression(
            inputLyr=layer,
            context=context,
            expression=expression,
            feedback=feedback
        )

    def prepareConversion(self, inputLyr, context, inputExpression=None, filterLyr=None,
                          behavior=None, bufferRadius=None, conversionMap=None, feedback=None):
        bufferRadius = 0 if bufferRadius is None else bufferRadius
        algRunner = AlgRunner()
        if feedback is not None:
            count = 0
            if inputExpression is not None:
                count += 1
            if filterLyr is not None:
                count += 1
                if behavior == 3:
                    count += 1
            elif count == 0:
                return inputLyr
            multiStepFeedback = QgsProcessingMultiStepFeedback(count, feedback)
        else:
            multiStepFeedback = None
        localLyr = inputLyr
        currentStep = 0
        if inputExpression is not None and inputExpression != '':
            if multiStepFeedback is not None:
                multiStepFeedback.setCurrentStep(currentStep)
            localLyr = algRunner.runFilterExpression(
                inputLyr=localLyr,
                context=context,
                expression=inputExpression,
                feedback=multiStepFeedback
            )
            currentStep += 1
        if filterLyr is not None:
            if multiStepFeedback is not None:
                multiStepFeedback.setCurrentStep(currentStep)
            if behavior == 3:
                filterLyr = algRunner.runBuffer(
                    filterLyr, bufferRadius, context, feedback=multiStepFeedback)
                currentStep += 1
            localLyr = algRunner.runIntersection(
                localLyr, context, overlayLyr=filterLyr)
        return localLyr

    def identifyAndFixInvalidGeometries(self, inputLyr, ignoreClosed=False, fixInput=False, onlySelected=False, feedback=None):
        iterator, featCount = self.getFeatureList(
            inputLyr, onlySelected=onlySelected)
        parameterDict = self.getDestinationParameters(inputLyr)
        geometryType = inputLyr.geometryType()
        flagDict, newFeatSet = self.identifyInvalidGeometries(iterator, featCount, inputLyr, ignoreClosed, fixInput, parameterDict, geometryType, feedback=feedback)
        if fixInput:
            self.applyGeometryFixesOnLayer(inputLyr, newFeatSet)

        return flagDict

    def identifyInvalidGeometries(self, iterator, featCount, inputLyr, ignoreClosed, fixInput, parameterDict, geometryType, feedback=None):
        flagDict = dict()
        newFeatSet = set()
        stepSize = 100/featCount if featCount else 0
        futures = set()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()-1)
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback) if feedback is not None else None
        if multiStepFeedback is not None:
            multiStepFeedback.setCurrentStep(0)
            multiStepFeedback.pushInfo(self.tr("Submitting tasks to thread..."))
        def evaluate(feat):
            _newFeatSet = set()
            geom = feat.geometry()
            id = feat.id()
            flagDict = self.checkGeomIsValid(geom, ignoreClosed, feedback)
            if fixInput:
                self.fixGeometryFromInput(inputLyr, parameterDict, geometryType, _newFeatSet, feat, geom, id)
            return flagDict, _newFeatSet
        for current, feat in enumerate(iterator):
            if feedback is not None and feedback.isCanceled():
                break
            futures.add(pool.submit(evaluate, feat))
            if feedback is not None:
                feedback.setProgress(stepSize*current)
        if multiStepFeedback is not None:
            multiStepFeedback.setCurrentStep(1)
            multiStepFeedback.pushInfo(self.tr("Evaluating results..."))
        for current, future in enumerate(concurrent.futures.as_completed(futures)):
            if feedback is not None and feedback.isCanceled():
                break
            output, _newFeatSet = future.result()
            if output:
                for point, errorDict in output.items():
                    if point in flagDict:
                        flagDict[point]["reason"] += errorDict["reason"]
                    else:
                        flagDict[point] = errorDict
            if _newFeatSet:
                newFeatSet = newFeatSet.union(_newFeatSet)
            if feedback is not None:
                feedback.setProgress(stepSize*current)
        return flagDict, newFeatSet

    def checkGeomIsValid(self, geom, ignoreClosed, feedback=None):
        flagDict = dict()
        for validate_type, method_parameter in {
            "GEOS": Qgis.GeometryValidationEngine.Geos,
            "QGIS": Qgis.GeometryValidationEngine.QgisInternal,
        }.items():
            if feedback is not None and feedback.isCanceled():
                break
            isValid = self.check_validity(ignoreClosed, flagDict, geom, validate_type, method_parameter)
            if not isValid:
                break
        if isValid and geom.type() == QgsWkbTypes.PolygonGeometry:
            self.analyze_polygon_boundary_and_holes(flagDict, geom)
        return flagDict

    def fixGeometryFromInput(self, inputLyr, parameterDict, geometryType, newFeatSet, feat, geom, id):
        attrMap = {idx: feat[field.name()] for idx, field in enumerate(
                    feat.fields()) if idx not in inputLyr.primaryKeyAttributes()}
        geom.removeDuplicateNodes(
                    useZValues=parameterDict['hasZValues'])
        fixedGeom = geom.makeValid()
        for idx, newGeom in enumerate(
                    self.geometryHandler.handleGeometryCollection(
                        fixedGeom, geometryType, parameterDict=parameterDict)
                    ):
            if idx == 0:
                inputLyr.changeGeometry(id, newGeom)
            else:
                newFeat = QgsVectorLayerUtils.createFeature(
                            inputLyr, newGeom, attrMap)
                newFeatSet.add(newFeat)

    def applyGeometryFixesOnLayer(self, inputLyr, newFeatSet):
        inputLyr.startEditing()
        inputLyr.beginEditCommand('Fixing geometries')
        inputLyr.addFeatures(newFeatSet)
        inputLyr.endEditCommand()

    def analyze_polygon_boundary_and_holes(self, flagDict, geom):
        flagWktSet = set()
        for part in geom.asGeometryCollection():
            if len(part.asPolygon()) <= 1:
                continue
            boundary, *holeList = map(lambda x: QgsGeometry.fromPolylineXY(x), part.asPolygon())
            for intersection in map(
                        lambda x: x.intersection(boundary),
                        filter(lambda y: y.intersects(boundary), holeList)
                    ):
                flagWktSet = flagWktSet.union(set(vertex.asWkt() for vertex in intersection.vertices()))
        for flag in flagWktSet:
            errorPointXY = QgsGeometry.fromWkt(flag).asPoint()
            flagGeom = QgsGeometry.fromPointXY(errorPointXY)
            if errorPointXY not in flagDict:
                flagDict[errorPointXY] = {
                                            'geom': flagGeom,
                                            'reason': ''
                                        }
            flagDict[errorPointXY]['reason'] += 'OGC invalid reason: {text}\n'.format(
                        text=self.tr("Self intersection between hole and boundary")
                    )

    def check_validity(self, ignoreClosed, flagDict, geom, validate_type, method_parameter):
        for error in geom.validateGeometry(method_parameter):
            if error.hasWhere():
                errorPointXY = error.where()
                flagGeom = QgsGeometry.fromPointXY(errorPointXY)
                if geom.type() == QgsWkbTypes.LineGeometry and ignoreClosed and\
                                self.isClosedAndFlagIsAtStartOrEnd(geom, flagGeom):
                    continue
                self._add_flag(flagDict, validate_type, error, errorPointXY, flagGeom)
                return False
        return True

    def _add_flag(self, flagDict, validate_type, error, errorPointXY, flagGeom):
        if errorPointXY not in flagDict:
            flagDict[errorPointXY] = {
                                'geom': flagGeom,
                                'reason': ''
                            }
        flagDict[errorPointXY]['reason'] += '{type} invalid reason: {text}\n'.format(
                            type=validate_type,
                            text=error.what()
                        )

    def isClosedAndFlagIsAtStartOrEnd(self, geom, flagGeom):
        for part in geom.asGeometryCollection():
            startPoint, endPoint = self.geometryHandler.getFirstAndLastNodeFromGeom(
                part)
            startPointGeom = QgsGeometry.fromPointXY(startPoint)
            endPointGeom = QgsGeometry.fromPointXY(endPoint)
            if not startPointGeom.equals(endPointGeom):
                continue
            if flagGeom.equals(startPointGeom) or flagGeom.equals(endPointGeom):
                return True
        return False

    def runGrassDissolve(self, inputLyr, context, feedback=None, column=None, outputLyr=None, onFinish=None):
        """
        Runs dissolve from GRASS algorithm provider.
        :param inputLyr: (QgsVectorLayer) layer to be dissolved.
        :param context: (QgsProcessingContext) processing context.
        :param feedback: (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        :param column: ()
        :param outputLyr: (str) URI to output layer.
        :param onFinish: (list-of-str) sequence of algs to be run after dissolve is executed, in execution order.
        :return: (QgsVectorLayer) dissolved (output) layer.
        """
        return AlgRunner().runGrassDissolve(inputLyr, context, feedback=None, column=None, outputLyr=None, onFinish=None)

    def getVertexNearEdgeDict(self, inputLyr, tol, onlySelected=False, feedback=None,
                              context=None, algRunner=None, ignoreErrorsOnSameFeat=False):
        """
        Identifies vertexes that are too close to a vertex.
        :param inputLyr: (QgsVectorLayer) layer to run the identification.
        :param onlySelected: (Boolean) If true, gets only selected layer
        :param tol: (float) search radius
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        if inputLyr.geometryType() == QgsWkbTypes.PointGeometry:
            raise Exception('Vertex near edge not defined for point geometry')
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(3, feedback)
        multiStepFeedback.setCurrentStep(0)
        multiStepFeedback.pushInfo(self.tr('Creating index'))
        usedInput = inputLyr if not onlySelected else QgsProcessingFeatureSourceDefinition(
            inputLyr.id(), True)
        incrementedLayer = algRunner.runAddAutoIncrementalField(
            usedInput,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.pushInfo(
            self.tr('Building auxiliar search structures'))
        edgeSpatialIdx, edgeIdDict = self.buildEdgesAuxStructure(
            incrementedLayer,
            feedback=multiStepFeedback,
            algRunner=algRunner,
            context=context
        )
        multiStepFeedback.setCurrentStep(2)
        multiStepFeedback.pushInfo(self.tr('Getting flags'))
        vertexNearEdgeFlagDict = self.getVertexNearEdgeFlagDict(
            incrementedLayer,
            edgeSpatialIdx,
            edgeIdDict,
            tol,
            feedback=multiStepFeedback,
            algRunner=algRunner,
            context=context,
            ignoreErrorsOnSameFeat=ignoreErrorsOnSameFeat
        )
        return vertexNearEdgeFlagDict

    def buildEdgesAuxStructure(self, inputLyr, algRunner=None, feedback=None, context=None):
        """
        returns a spatialIndex of lines and a dict of the features
        :param inputLyr: (QgsVectorLayer) layer to run build the aux structure.
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        nSteps = 3 if inputLyr.geometryType() == QgsWkbTypes.PolygonGeometry else 2
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(nSteps, feedback)
        currentStep = 0
        multiStepFeedback.setCurrentStep(currentStep)
        edgeLyr = inputLyr if inputLyr.geometryType() == QgsWkbTypes.LineGeometry \
            else algRunner.runPolygonsToLines(
            inputLyr,
            context,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        explodedEdges = algRunner.runExplodeLines(
            edgeLyr,
            context,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        spatialIdx, idDict = self.buildSpatialIndexAndIdDict(
            explodedEdges,
            feedback=multiStepFeedback
        )
        return spatialIdx, idDict

    def getVertexNearEdgeFlagDict(self, inputLyr, edgeSpatialIdx, edgeIdDict,
                                  searchRadius, feedback=None, algRunner=None, context=None, ignoreErrorsOnSameFeat=False):
        """
        returns a dict in the following format:
            {'featid':{
                'vertexWkt': {
                    'flagGeom' : --geometry of the flag--,
                    'edges' : set of edges (QgsGeometry)
                }

            }
            } 
        :param inputLyr: (QgsVectorLayer) layer to run build the aux structure.
        :param edgeSpatialIdx: (QgsSpatialIndex) spatial index to perform the search
        :param edgeIdDict: (dict) dictionary in the format {featid:QgsFeature}
        :param searchRadius: (float) search radius
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        flagDict = defaultdict(lambda: defaultdict(lambda: {'edges': set()}))
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
        # step 1: extract vertexes
        multiStepFeedback.setCurrentStep(0)
        vertexLyr = algRunner.runExtractVertices(
            inputLyr,
            context,
            feedback=multiStepFeedback
        )
        # step 2: for each vertex, get the buffer, the buffer BoundingBox and
        # assess wich edge intersects the buffer. If there is any, this is a flag
        multiStepFeedback.setCurrentStep(1)
        iterator, size = self.getFeatureList(
            vertexLyr, returnIterator=True, onlySelected=False)
        if size == 0:
            return {}
        stepSize = 100/size
        for current, pointFeat in enumerate(iterator):
            if multiStepFeedback.isCanceled():
                break
            pointGeom = pointFeat.geometry()
            buffer = pointGeom.buffer(searchRadius, -1)
            bufferBB = buffer.boundingBox()
            featId = pointFeat['featid']
            # pointWkt is used as a key because it is unique and hashable
            pointWkt = pointGeom.asWkt()
            for candidateId in edgeSpatialIdx.intersects(bufferBB):
                if multiStepFeedback.isCanceled():
                    break
                edgeGeom = edgeIdDict[candidateId].geometry()
                # must ignore search within the same feature and
                # must be with not adjacent edges
                if pointGeom.touches(edgeGeom) or \
                    (ignoreErrorsOnSameFeat and featId == edgeIdDict[candidateId]['featid'] and
                        pointFeat['layer'] == edgeIdDict[candidateId]['layer']):
                    continue
                if buffer.intersects(edgeGeom):
                    flagDict[featId][pointWkt]['flagGeom'] = pointGeom
                    flagDict[featId][pointWkt]['edges'].add(edgeGeom)
            # make progress
            multiStepFeedback.setProgress(current * stepSize)
        return flagDict

    def getUnsharedVertexOnSharedEdgesDict(self, inputLineLyrList, inputPolygonLyrList, searchRadius, onlySelected=False, feedback=None, context=None, algRunner=None):
        """
        returns a dict in the following format:
            {'featid':{
                'vertexWkt': {
                    'flagGeom' : --geometry of the flag--,
                    'edges' : set of edges (QgsGeometry)
                }

            }
            } 
        :param inputLineLyrList: (list of QgsVectorLayers) line layers to run build the aux structure.
        :param inputPolygonLyrList: (list of QgsVectorLayers) line polygon layers to run build the aux structure.
        :param searchRadius: (float) search radius
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        inputList = inputLineLyrList
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(3, feedback)
        multiStepFeedback.setCurrentStep(0)
        multiStepFeedback.pushInfo(self.tr('Getting lines'))
        linesLyr = self.getLinesLayerFromPolygonsAndLinesLayers(
            inputLineLyrList,
            inputPolygonLyrList,
            onlySelected=onlySelected,
            feedback=multiStepFeedback,
            context=context
        )
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.pushInfo(self.tr('Building vertex near edge dict'))
        # only selected should not be filled because it was already used to build the line lyr
        return self.getVertexNearEdgeDict(
            linesLyr,
            searchRadius,
            algRunner=algRunner,
            feedback=multiStepFeedback,
            context=context,
            ignoreErrorsOnSameFeat=True
        )

    def getUnsharedVertexOnIntersections(self, pointLineLyrList, inputLineLyrList, inputPolygonLyrList,
                                         onlySelected=False, feedback=None, context=None, algRunner=None):
        """
        returns a dict in the following format:
            {
                '--flag Wkt--' : {
                    'layer1' : --layer 1 name--',
                    'layer2' : --layer 2 name--'
                }
            } 
        :param inputLineLyrList: (list of QgsVectorLayers) line layers to run build the aux structure.
        :param inputPolygonLyrList: (list of QgsVectorLayers) line polygon layers to run build the aux structure.
        :param searchRadius: (float) search radius
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        stepCount = 6 if pointLineLyrList else 5
        multiStepFeedback = QgsProcessingMultiStepFeedback(stepCount, feedback)
        currentStep = 0
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.pushInfo(self.tr('Getting lines'))
        linesLyr = self.getLinesLayerFromPolygonsAndLinesLayers(
            inputLineLyrList,
            inputPolygonLyrList,
            onlySelected=onlySelected,
            feedback=multiStepFeedback,
            context=context
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.pushInfo(self.tr('Building point merged layer'))
        pointsLyr = algRunner.runMergeVectorLayers(pointLineLyrList, context, feedback=multiStepFeedback) if pointLineLyrList else None
        currentStep += 1

        if pointsLyr is not None:
            multiStepFeedback.setCurrentStep(currentStep)
            pointsLyr = algRunner.runMultipartToSingleParts(
                inputLayer=pointsLyr, context=context, feedback=multiStepFeedback
            )
            currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.pushInfo(self.tr('Building intersections'))
        intersectionLyr = algRunner.runLineIntersections(
            linesLyr,
            linesLyr,
            feedback=multiStepFeedback,
            context=context
        )
        currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.pushInfo(self.tr('Finding vertexes'))
        vertexLyr = algRunner.runExtractVertices(
            linesLyr,
            feedback=multiStepFeedback,
            context=context
        )
        currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.pushInfo(self.tr('Finding unshared vertexes'))
        intersectionDict = {
            feat.geometry().asWkb(): feat for feat in intersectionLyr.getFeatures()
        }
        unsharedPointsSet = self.getUnsharedPointsSetFromPointsLyr(algRunner, pointsLyr, linesLyr, context, multiStepFeedback)
        vertexSet = set(
            feat.geometry().asWkb() for feat in vertexLyr.getFeatures()
        )
        return set(intersectionDict.keys()).difference(vertexSet) | unsharedPointsSet.difference(vertexSet)
    
    def getUnsharedPointsSetFromPointsLyr(self, algRunner, pointsLyr, linesLyr, context, feedback):
        if pointsLyr is None or pointsLyr.featureCount() == 0:
            return set()
        multiStepFeedback = QgsProcessingMultiStepFeedback(2, feedback)
        multiStepFeedback.setCurrentStep(0)
        algRunner.runCreateSpatialIndex(linesLyr, context, feedback=multiStepFeedback)
        multiStepFeedback.setCurrentStep(1)
        def compute(feat):
            geom = feat.geometry()
            geomWkb = geom.asWkb()
            buffer = geom.buffer(1e-8, -1)
            geomEngine = QgsGeometry.createGeometryEngine(buffer.constGet())
            for lineFeat in linesLyr.getFeatures(geom.boundingBox()):
                if geomEngine.intersects(lineFeat.geometry().constGet()):
                    hasVertex = any(geom.equals(QgsGeometry(i)) for i in lineFeat.geometry().vertices())
                    if not hasVertex:
                        return geomWkb
            return None
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()-1)
        futures = set()
        vertexSet = set()
        for feat in pointsLyr.getFeatures():
            # futures.add(pool.submit(compute, feat))
            result = compute(feat)
            if result is not None:
                vertexSet.add(result)
        # vertexSet = set(
        #     future.result() for future in concurrent.futures.as_completed(
        #         futures
        #     ) if future.result() is not None
        # )
        return vertexSet

    def getLinesLayerFromPolygonsAndLinesLayers(self, inputLineLyrList, inputPolygonLyrList, algRunner=None, onlySelected=False, feedback=None, context=None):
        """
        returns a merged line lyr
        :param inputLineLyrList: (list of QgsVectorLayers) line layers to run build the aux structure.
        :param inputPolygonLyrList: (list of QgsVectorLayers) line polygon layers to run build the aux structure.
        :param feedback (QgsProcessingFeedback) QGIS object to keep track of progress/cancelling option.
        """
        lineList = []
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        nSteps = len(inputLineLyrList) + 2*len(inputPolygonLyrList) + 1
        multiStepFeedback = QgsProcessingMultiStepFeedback(
            nSteps, feedback)  # set number of steps
        currentStep = 0
        for lineLyr in inputLineLyrList:
            multiStepFeedback.setCurrentStep(currentStep)
            lineList.append(
                lineLyr if not onlySelected else
                algRunner.runSaveSelectedFeatures(
                    lineLyr,
                    context,
                    feedback=multiStepFeedback
                )
            )
            currentStep += 1
        for polygonLyr in inputPolygonLyrList:
            multiStepFeedback.setCurrentStep(currentStep)
            usedInput = polygonLyr if not onlySelected else \
                QgsProcessingFeatureSourceDefinition(polygonLyr.id(), True)
            convertedPolygons = algRunner.runPolygonsToLines(
                usedInput,
                context,
                feedback=multiStepFeedback
            )
            currentStep += 1
            multiStepFeedback.setCurrentStep(currentStep)
            explodedLines = algRunner.runExplodeLines(convertedPolygons, context, feedback=multiStepFeedback)
            currentStep += 1
            lineList.append(explodedLines)
        # merge layers
        multiStepFeedback.setCurrentStep(currentStep)
        mergedLayer = algRunner.runMergeVectorLayers(
            lineList,
            context,
            feedback=multiStepFeedback
        )
        return mergedLayer

    def reprojectLayer(self, layer, targetEpsg, output=None):
        """
        Reprojects layer's CRS.
        :param : (QgsVectorLayer) layer to be reprojected.
        :param targetCrs: (QgsCoordinateReferenceSystem) CRS object for the
                          output layer.
        :param output: (QgsVectorLayer) layer accomodate reprojected layer.
        :return: (QgsVectorLayer) reprojected layer
        """
        return AlgRunner().runReprojectLayer(layer, targetEpsg, output)

    def getMergedLayer(self, inputLayerList, onlySelected=False, feedback=None, context=None, algRunner=None):
        """
        This does almost the same of createAndPopulateUnifiedVectorLayer, but it
        is much faster. Maybe the implementation of createAndPopulateUnifiedVectorLayer
        should change.
        """
        lyrList = []
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        nSteps = len(inputLayerList) + 1
        multiStepFeedback = QgsProcessingMultiStepFeedback(
            nSteps, feedback)  # set number of steps
        currentStep = 0
        for lyr in inputLayerList:
            multiStepFeedback.setCurrentStep(currentStep)
            lyrList.append(
                lyr if not onlySelected else algRunner.runSaveSelectedFeatures(
                    lineLyr,
                    context,
                    feedback=multiStepFeedback
                )
            )
            currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        return algRunner.runMergeVectorLayers(
            lyrList,
            context,
            feedback=multiStepFeedback
        )

    def getCentroidsAndBoundariesFromPolygons(self, inputLyr, outputCenterPointSink, outputBoundarySink,
                                              constraintLineLyrList=None, constraintPolygonLyrList=None, context=None, feedback=None, algRunner=None):
        """
        FILL out
        """
        constraintLineLyrList = [] if constraintLineLyrList is None else constraintLineLyrList
        constraintPolygonLyrList = [
        ] if constraintPolygonLyrList is None else constraintPolygonLyrList
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(7, feedback)
        multiStepFeedback.setCurrentStep(0)
        multiStepFeedback.pushInfo(self.tr('Getting constraint lines'))
        linesLyr = self.getLinesLayerFromPolygonsAndLinesLayers(
            constraintLineLyrList,
            constraintPolygonLyrList,
            onlySelected=False,
            feedback=multiStepFeedback,
            context=context,
            algRunner=algRunner
        )
        multiStepFeedback.setCurrentStep(1)
        multiStepFeedback.pushInfo(
            self.tr('Building auxiliar search structures'))
        constraintSpatialIdx, constraintIdDict = self.buildSpatialIndexAndIdDict(
            linesLyr,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(2)
        edgeLyr = algRunner.runPolygonsToLines(
            inputLyr,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(3)
        explodedEdges = algRunner.runExplodeLines(
            edgeLyr,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(4)
        explodedWithoutDuplicates = algRunner.runRemoveDuplicatedGeometries(
            explodedEdges,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(5)
        self.buildCenterPoints(
            inputLyr,
            outputCenterPointSink,
            explodedWithoutDuplicates,
            linesLyr,
            feedback=multiStepFeedback,
            context=context,
            algRunner=algRunner
        )
        multiStepFeedback.setCurrentStep(6)
        self.filterEdges(
            explodedWithoutDuplicates,
            constraintSpatialIdx,
            constraintIdDict,
            outputBoundarySink,
            feedback=multiStepFeedback,
            context=context
        )

    def buildCenterPoints(self, inputLyr, outputCenterPointSink, polygonBoundaryLyr,
                          constraintLineLyr=None, context=None, feedback=None, algRunner=None):
        """

        """
        # 1- Merge line layers
        # 2- Build polygons
        # 3- Get center points from built polygons
        # 4- Make spatial join of center points with original polygons to get attributes
        algRunner = AlgRunner() if algRunner is None else algRunner
        context = dataobjects.createContext(
            feedback=feedback) if context is None else context
        multiStepFeedback = QgsProcessingMultiStepFeedback(6, feedback)
        multiStepFeedback.setCurrentStep(0)
        mergedLineLyr = polygonBoundaryLyr if constraintLineLyr is None\
            else algRunner.runMergeVectorLayers(
                [polygonBoundaryLyr, constraintLineLyr],
                context,
                feedback=multiStepFeedback
            )
        multiStepFeedback.setCurrentStep(1)
        splitSegmentsLyr = algRunner.runExplodeLines(
            mergedLineLyr,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(2)
        segmentsWithoutDuplicates = algRunner.runRemoveDuplicatedGeometries(
            splitSegmentsLyr,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(3)
        outputPolygonLyr = algRunner.runPolygonize(
            segmentsWithoutDuplicates,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(4)
        centroidLyr = algRunner.runPointOnSurface(
            outputPolygonLyr,
            context,
            feedback=multiStepFeedback
        )
        multiStepFeedback.setCurrentStep(5)
        centroidsWithAttributes = algRunner.runJoinAttributesByLocation(
            centroidLyr,
            inputLyr,
            context,
            feedback=multiStepFeedback
        )
        for feat in centroidsWithAttributes.getFeatures():
            outputCenterPointSink.addFeature(feat, QgsFeatureSink.FastInsert)

    def filterEdges(self, inputLyr, constraintSpatialIdx, constraintIdDict,
                    outputBoundarySink, context=None, feedback=None, algRunner=None):
        """
        """
        notBoundarySet = set()
        stepSize = 100/inputLyr.featureCount()
        featList = [i for i in inputLyr.getFeatures()]
        for current, feat in enumerate(featList):
            if feedback is not None and feedback.isCanceled():
                break
            featGeom = feat.geometry()
            featBB = featGeom.boundingBox()
            for candidateId in constraintSpatialIdx.intersects(featBB):
                if featGeom.within(constraintIdDict[candidateId].geometry()):
                    notBoundarySet.add(feat)
                    break
            if feedback is not None:
                feedback.setProgress(current*stepSize)
        for feat in featList:
            if feat not in notBoundarySet:
                outputBoundarySink.addFeature(feat, QgsFeatureSink.FastInsert)

    def getPolygonsFromCenterPointsAndBoundaries(self, inputCenterPointLyr, 
                                                constraintLineLyrList=None,
                                                constraintPolygonLyrList=None, 
                                                attributeBlackList=None, 
                                                geographicBoundaryLyr=None,
                                                onlySelected=False,
                                                suppressPolygonWithoutCenterPointFlag=False,
                                                context=None, 
                                                feedback=None, 
                                                algRunner=None):
        """

        1. Merge Polygon lyrs into one and coerce polygons to lines
        2. Merge all lines
        3. Split lines
        4. Run Polygonize
        5. Get Flags, filtering them with constraint polygons

        :params inputCenterPointLyr: Layer with Point which you want to take
            the attr
        :params constraintLineLyrList: (list) with layers to be considered when
            when making the polygons
        :params constraintPolygonLyrList: (list) with the polygons, which area
            should not be considered
        :params attributeBlackList: (list) attr columns which are unnecessary
        :params geographicBoundary: layer which delimitates the processing
        :return polygonList, flagList: list of polygons (QgsFeature)
        """
        algRunner = AlgRunner() if algRunner is None else algRunner
        constraintLineLyrList = [] if constraintLineLyrList is None else \
                                constraintLineLyrList
        constraintPolygonList = [] if constraintPolygonLyrList is None else \
                                constraintPolygonLyrList
        attributeBlackList = [] if attributeBlackList is None else \
                                attributeBlackList
        # Clip Points, Lines and Polygons according to geographicBoundaryLyr
        # Buffer because sometimes the line stops before the boundary itself,
        # making the whole algorithmn not work properly
        # if geographicBoundaryLyr:
        #     limit = algRunner.runBuffer(geographicBoundaryLyr,0.00001,context)
        #     constraintLineLyrList = [algRunner.runClip(camada, limit, context)
        #                             for camada in constraintLineLyrList]
        #     constraintPolygonList = [algRunner.runClip(camada, limit, context)
        #                             for camada in constraintPolygonLyrList]
        #     inputCenterPointLyr = algRunner.runClip(inputCenterPointLyr, limit, 
        #                                             context)
        constraintPolygonListWithGeoBounds = constraintPolygonList + \
            [geographicBoundaryLyr] if geographicBoundaryLyr is not None else \
            constraintPolygonList
        multiStepFeedback = QgsProcessingMultiStepFeedback(7, feedback)
        # 1. Merge Polygon lyrs into one
        currentStep = 0
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.setProgressText(self.tr('Getting constraint lines...'))
        linesLyr = self.getLinesLayerFromPolygonsAndLinesLayers(
            constraintLineLyrList,
            constraintPolygonListWithGeoBounds,        
            onlySelected=onlySelected,
            feedback=multiStepFeedback,
            context=context,
            algRunner=algRunner
        )
        currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.setProgressText(self.tr('Exploding lines...'))
        algRunner.runCreateSpatialIndex(linesLyr, context, feedback=multiStepFeedback)
        currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        linesLyr = algRunner.runSplitLinesWithLines(
            inputLyr=linesLyr,
            linesLyr=linesLyr,
            context=context,
            feedback=multiStepFeedback
        )
        currentStep += 1

        multiStepFeedback.setCurrentStep(currentStep)
        splitSegmentsLyr = algRunner.runExplodeLines(
            linesLyr,
            context,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.setProgressText(self.tr('Removing duplicated features...'))
        segmentsWithoutDuplicates = algRunner.runRemoveDuplicatedGeometries(
            splitSegmentsLyr,
            context,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.setProgressText(self.tr('Starting the process of building polygons...'))
        builtPolygonLyr = algRunner.runPolygonize(
            segmentsWithoutDuplicates,
            context,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        multiStepFeedback.setProgressText(self.tr('Relating center points with built polygons...'))
        return self.relateCenterPointsWithPolygons(
            inputCenterPointLyr,
            builtPolygonLyr,
            constraintPolygonList=constraintPolygonList,
            geomBoundary = geographicBoundaryLyr,
            attributeBlackList=attributeBlackList,
            suppressPolygonWithoutCenterPointFlag=suppressPolygonWithoutCenterPointFlag,
            context=context,
            feedback=multiStepFeedback
        )

    def relateCenterPointsWithPolygons(self,inputCenterPointLyr, 
                                        builtPolygonLyr,context=None,
                                        constraintPolygonList=None, 
                                        attributeBlackList=None,
                                        geomBoundary=None,
                                        suppressPolygonWithoutCenterPointFlag=False,
                                        feedback=None):
        """
        1. Merge constraint polygon list;
        2. Build search structure into constraint polygon list
        3. Build structure relating center points to built polygons
        4. Get built polygons with attributes and flags

        :params inputCenterPointLyr: (QgsVectorLayer) with Point which you 
            want to take the attr
        :params builtPolygonLyr: (QgsVectorLayer) with the polygons
        :params constraintPolygonList: (list) with the polygons, which area
            should not be considered
        :params attributeBlackList: (list) attr columns which are unnecessary
        :params geomBoundary: (QgsVectorLayer) which delimitates the processing
        :return polygonList, flagList: list of polygons (QgsFeature)
        """
        nSteps = 4 if constraintPolygonList else 2
        multiStepFeedback = QgsProcessingMultiStepFeedback(nSteps, feedback)
        currentStep = 0
        multiStepFeedback.setCurrentStep(currentStep)
        
        # If there were no polygon list, it was breaking the method. 
        # tried to solve it with this if else 
        if constraintPolygonList:
            constraintPolygonLyr = self.algRunner.runMergeVectorLayers(
                constraintPolygonList,
                context,
                feedback=multiStepFeedback
            )
            currentStep += 1
            multiStepFeedback.setCurrentStep(currentStep)
            constraintPolygonLyrSpatialIdx, constraintPolygonLyrIdDict = \
            self.buildSpatialIndexAndIdDict(
                constraintPolygonLyr,
                feedback=multiStepFeedback
            )
            currentStep += 1
        else: 
            constraintPolygonLyrSpatialIdx, constraintPolygonLyrIdDict = \
                QgsSpatialIndex(), {}
        multiStepFeedback.setCurrentStep(currentStep)
        builtPolygonToCenterPointDict = self.buildCenterPolygonToCenterPointDict(
            inputCenterPointLyr,
            builtPolygonLyr,
            attributeBlackList,
            feedback=multiStepFeedback
        )
        currentStep += 1
        multiStepFeedback.setCurrentStep(currentStep)
        polygonList, flagList = \
        self.getPolygonListAndFlagDictFromBuiltPolygonToCenterPointDict(
            builtPolygonToCenterPointDict,
            constraintPolygonLyrSpatialIdx,
            constraintPolygonLyrIdDict,
            geomBoundary = geomBoundary,
            suppressPolygonWithoutCenterPointFlag=suppressPolygonWithoutCenterPointFlag,
            feedback=multiStepFeedback
        )
        return polygonList, flagList

    def buildCenterPolygonToCenterPointDict(self, inputCenterPointLyr, 
                                            builtPolygonLyr,
                                            attributeBlackList=None, 
                                            feedback=None):
        """
        
        :params inputCenterPointLyr: (QgsVectorLayer) with Point which you want to take
            the attr
        :params builtPolygonLyr: (QgsVectorLayer) with the polygons
        :params attributeBlackList: (list) attr columns which are unnecessary
        :return builtPolygonToCenterPointDict: (dict) in the following format:
        {
            'geomWkb' : {
                (attrKey) : [--QgsFields--]
            }
        }
        """
        builtPolygonToCenterPointDict = dict()
        iterator, featCount = self.getFeatureList(
            builtPolygonLyr, onlySelected=False)
        size = 100/featCount if featCount else 0
        columns = self.getAttributesFromBlackList(
            inputCenterPointLyr, attributeBlackList=attributeBlackList)
        fields = self.getFieldsFromAttributeBlackList(
            inputCenterPointLyr, attributeBlackList)  
        for current, feat in enumerate(builtPolygonLyr.getFeatures()):
            if feedback is not None and feedback.isCanceled():
                break
            featGeom = feat.geometry()
            geomKey = featGeom.asWkb()
            # creating dict entry
            if geomKey not in builtPolygonToCenterPointDict:
                builtPolygonToCenterPointDict[geomKey] = defaultdict(list)
            featBB = featGeom.boundingBox()
            request = QgsFeatureRequest().setFilterRect(featBB)
            engine = QgsGeometry.createGeometryEngine(featGeom.constGet())
            engine.prepareGeometry()
            
            # for each point inside the polygon, extract attrkey and attr
            # attrkey is basically attr but in an string. therefore, if 
            # if there two point with different attr, it will make two columns
            # with two attrkey different            
            for pointFeat in inputCenterPointLyr.getFeatures(request):
                if feedback is not None and feedback.isCanceled():
                    break
                if engine.intersects(pointFeat.geometry().constGet()):
                    attr = tuple((pointFeat[f] for f in columns))
                    # for index in listColumnAttr:
                    #     attr.append(pointFeat.attributes()[index])
                    # attr = tuple(attr)
                    builtPolygonToCenterPointDict[geomKey][attr] = fields
                if feedback is not None:
                    feedback.setCurrentStep(current * size)
        return builtPolygonToCenterPointDict

    def getFieldsFromAttributeBlackList(self, originalLayer, 
                                        attributeBlackList):
        """
        Create a QgsFields with only columns that are not at attributeBlackList
        :params originalLayer: Layer from where will be taken the fields
        :params attributeBlackList: (list) which attr/fields should not 
                                    be considered
        :return fields: (QgsFields) with the fields necessary   
        """
        columns = self.getAttributesFromBlackList(
            originalLayer,
            attributeBlackList=attributeBlackList
        )
        fields = QgsFields()
        for f in originalLayer.fields():
            if f.name() in columns:
                fields.append(f)
        return fields

    def getListIndexFromFields(self, originalLayer, columns):
        """
        Create a list which contains the indexes of the attr that are required
        :params originalLayer: Layer from where will be taken the fields
        :params columns: (list) list of desirable columns
        :return listColumnAttr: (list) list of indexes
        """
        listColumnAttr = []
        for column in columns: 
            listColumnAttr.append(originalLayer.fields()\
                                            .indexFromName(column))
        return listColumnAttr

    def getPolygonListAndFlagDictFromBuiltPolygonToCenterPointDict(
            self, builtPolygonToCenterPointDict,
            constraintPolygonLyrSpatialIdx, constraintPolygonLyrIdDict,
            geomBoundary=False, suppressPolygonWithoutCenterPointFlag=True, feedback=None):
        """
        :params builtPolygonToCenterPointDict: (dict) in the following format:
        {
            'geomWkb' : {
                (attrKey) : [--QgsFields--]
            }
        }
        :params constraintPolygonLyrSpatialIdx :QgsSpatialIndex() of the 
        constraint polygons
        :params constraintPolygonLyrIdDict: 
        :params geomBoundary: (QgsVectorLayer) which delimitates the processing
        :return polygonList, flagList: *list) of polygons (QgsFeature)
        """
        keyCount = len(builtPolygonToCenterPointDict)
        size = 100/keyCount if keyCount else 0
        polygonList = []
        flagDict = dict()
        # Create a list with geomBoundary polygons geometry 
        if geomBoundary:
            geoms = [f.geometry() for f in geomBoundary.getFeatures()]
        for current, geomKey in enumerate(builtPolygonToCenterPointDict):
            if feedback is not None and feedback.isCanceled():
                break
            structureLen = len(builtPolygonToCenterPointDict[geomKey])
            geom = QgsGeometry()
            geom.fromWkb(geomKey)
            insideConstraint = False
            pointOnSurfaceGeom = geom.pointOnSurface()
            # only situation when boundary makes a difference is when it is
            # provided and the polygon is not within any of its polygons
            isOffBoundary = geomBoundary is not None \
                and not any(
                    (pointOnSurfaceGeom.intersects(boundaryGeom) \
                        for boundaryGeom in geoms)
                )
            if isOffBoundary:
                continue
            for candidateId in constraintPolygonLyrSpatialIdx.intersects(
                                    geom.boundingBox()):
                # this loop is broken by either user canceling it or built
                # polygon overlapping any of the polygon constraints, which
                # means that it will have its geometry ignored on the next
                # 'else' block => it is neither a final built nor a flag, just
                # ignored
                if feedback is not None and feedback.isCanceled():
                    break
                isNotFlag = geomKey not in flagDict
                isConstraintPol = pointOnSurfaceGeom.intersects(
                    constraintPolygonLyrIdDict[candidateId].geometry())
                if isNotFlag and isConstraintPol:
                    break
            else:
                # because builtPolygonToCenterPointDict[geomKey] is an 
                # defaultdict, everytime that it appends a different set of
                # attr, it creates a new column. Therefore, when 
                # structureLen is 0, there is no attr, when is more than 1
                # it had two points with differents attr. 
                if structureLen == 1:
                    fields = list(builtPolygonToCenterPointDict[geomKey].values())
                    attr = list(builtPolygonToCenterPointDict[geomKey].keys())
                    newFeat = QgsFeature(fields[0])
                    newFeat.setAttributes(list(attr[0]))
                    newFeat.setGeometry(geom)
                    polygonList.append(newFeat)
                else:
                    if structureLen == 0 and suppressPolygonWithoutCenterPointFlag:
                        continue
                    flagText = self.tr("Polygon without center point.") \
                        if structureLen == 0 \
                        else self.tr(
                            "Polygon with more than one center point with "
                            "conflicting attributes."
                        )
                    flagDict[geomKey] = flagText
            if feedback is not None:
                feedback.setCurrentStep(current * size)
        return polygonList, flagDict

    def valueMaps(self, layer):
        """
        Gets the value maps for each field of a given layer, if available.
        :param layer: (QgsVectorLayer) layer to have its fields read.
        :return: (dict) value maps for each layer field that has it.
        """
        classFieldMap = dict()
        layername = layer.name()
        for field in layer.fields():
            fieldName = field.name()
            fieldConfig = field.editorWidgetSetup().config()
            if 'map' not in fieldConfig or fieldName in ('UseHtml', 'IsMultiline'):
                continue
            if isinstance(fieldConfig['map'], list):
                for map_ in fieldConfig['map']:
                    if fieldName not in classFieldMap:
                        classFieldMap[fieldName] = map_
                    else:
                        classFieldMap[fieldName].update(map_)
            else:
                def intify(i):
                    try:
                        return int(i)
                    except:
                        return i

                def sortingMethod(item):
                    return intify(item[1])
                classFieldMap[fieldName] = {
                    k: intify(v) for k, v in sorted(
                        fieldConfig['map'].items(), key=sortingMethod
                    )
                }
        return classFieldMap

    def getDefaultValues(self, layer):
        """
        Gets the default values for all fields as from the provider.
        :param layer: (QgsVectorLayer) layer to have its default values read.
        :return: (dict) a map from attribute name to its default value.
        """
        provider = layer.dataProvider()
        fields = layer.fields()
        defaultValues = dict()
        for idx, f in enumerate(fields):
            if fields.fieldOrigin(idx) == fields.OriginExpression:
                # ignore virtual fields
                continue
            providerIdx = fields.fieldOriginIndex(idx)
            defaultValues[f.name()] = provider.defaultValueClause(providerIdx)
        return defaultValues

    def getPolygonSlivers(
            self, layer, ratio, selected=False, silent=False, feedback=None):
        """
        Identifies the polygon slivers for a given ratio area-perimeter
        of a layer.
        :param layer: (QgsVectorLayer) layer to be checked.
        :param ratio: (float) ratio area-perimeter to be used as tolerance.
        :param selected: (bool) whether exclusively selected features from
                         input layer should be checked for polygon slivers.
        :param silent: (bool) whether an invalid or empty geometry should be
                       ignored.
        :param feedback: (QgsProcessingFeedback) QGIS object to keep track of
                         algorithm's progress/status.
        :return: (list-of-QgsFeature) list of all polygon slivers found in the
                 in the input layer.
        """
        if not layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            Exception(self.tr("Input layer is not polygon."))
        slivers = list()
        feats = list(layer.getSelectedFeatures() if selected \
            else layer.getFeatures())
        stepSize = 100 / len(feats) if feats else 0
        for step, f in enumerate(feats):
            geom = f.geometry()
            if geom.length() == 0 or not geom.isGeosValid():
                if not silent:
                    raise Exception(
                        self.tr("Invalid or empty geometry found!"))
                feedback.setProgress((step + 1) * stepSize)
                continue
            if geom.area() / (geom.length())**2 < ratio:
                slivers.append(f)
            if feedback is not None:
                if feedback.isCanceled():
                    return slivers
                feedback.setProgress((step + 1) * stepSize)
        return slivers
