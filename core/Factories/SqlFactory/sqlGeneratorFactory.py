# -*- coding: utf-8 -*-
"""
/***************************************************************************
 DsgTools
                                 A QGIS plugin
 Brazilian Army Cartographic Production Tools
                              -------------------
        begin                : 2014-11-08
        git sha              : $Format:%H$
        copyright            : (C) 2014 by Luiz Andrade - Cartographic Engineer @ Brazilian Army
        email                : luiz.claudio@dsg.eb.mil.br
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
from builtins import object
import os

from .spatialiteSqlGenerator import SpatialiteSqlGenerator
from .postgisSqlGenerator import PostGISSqlGenerator

class SqlGeneratorFactory(object):
    def createSqlGenerator(self, isSpatialite):
        """
        Returns the specific sql generator
        :param isSpatialite:
        :return:
        """
        if isSpatialite:
            return SpatialiteSqlGenerator()
        else:
            return PostGISSqlGenerator()