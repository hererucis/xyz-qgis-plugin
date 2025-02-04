# -*- coding: utf-8 -*-
###############################################################################
#
# Copyright (c) 2019 HERE Europe B.V.
#
# SPDX-License-Identifier: MIT
# License-Filename: LICENSE
#
###############################################################################

import sqlite3
import time
import json

from qgis.core import (QgsCoordinateReferenceSystem, QgsFeatureRequest,
                       QgsProject, QgsVectorFileWriter, QgsVectorLayer, 
                       QgsCoordinateTransform, QgsWkbTypes)

from qgis.utils import iface
from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.PyQt.QtXml import QDomDocument

from . import parser, render
from .layer_utils import get_feat_cnt_from_src
from ...models.space_model import parse_copyright
from ...models import SpaceConnectionInfo
from ...utils import make_unique_full_path, make_fixed_full_path
from .style import LAYER_QML

from ..common.signal import make_print_qgis
print_qgis = make_print_qgis("layer")



class XYZLayer(object):
    """ XYZ Layer is created in 2 scenarios:
    + loading a new layer from xyz
    + uploading a qgis layer to xyz, add conn_info, meta, vlayer
    """
    NO_GEOM = "No geometry"
    GEOM_ORDER = dict((k,i) 
    for i,k in enumerate(["Point","Line","Polygon", "Unknown geometry", NO_GEOM]))
    # https://qgis.org/api/qgswkbtypes_8cpp_source.html#l00129 

    def __init__(self, conn_info, meta, tags="", unique=None, group_name="XYZ Hub Layer", ext="gpkg"):
        super().__init__()
        self.ext = ext
        self.conn_info = conn_info
        self.meta = meta
        self.tags = tags
        self.unique = unique or int(time.time() * 10)
        self._group_name = group_name

        self.map_vlayer = dict()
        self.map_fields = dict()
        self.qgroups = dict()

    @classmethod
    def load_from_qnode(cls, qnode):
        meta = qnode.customProperty("xyz-hub")
        conn_info = qnode.customProperty("xyz-hub-conn")
        tags = qnode.customProperty("xyz-hub-tags")
        unique = qnode.customProperty("xyz-hub-id")
        name = qnode.name()
        meta = json.loads(meta)
        conn_info = json.loads(conn_info)
        conn_info = SpaceConnectionInfo.from_dict(conn_info)

        obj = cls(conn_info, meta, tags=tags, unique=unique, group_name=name)
        obj.qgroups["main"] = qnode
        for g in qnode.findGroups():
            obj.qgroups[g.name()] = g
            lst_vlayers = [i.layer() for i in g.findLayers()]
            for vlayer in lst_vlayers:
                geom_str = QgsWkbTypes.displayString(vlayer.wkbType())
                obj.map_vlayer.setdefault(geom_str, list()).append(vlayer)
                obj.map_fields.setdefault(geom_str, list()).append(vlayer.fields())
                
        return obj

    def _save_meta_node(self, qnode):
        qnode.setCustomProperty("xyz-hub", json.dumps(self.meta, ensure_ascii=False))
        qnode.setCustomProperty("xyz-hub-conn", json.dumps(self.conn_info.to_dict(), ensure_ascii=False))
        qnode.setCustomProperty("xyz-hub-tags", self.tags)
        qnode.setCustomProperty("xyz-hub-id", self.get_id())

    def _save_meta(self, vlayer):
        self._save_meta_node(vlayer)

        lic = self.meta.get("license")
        cr = self.meta.get("copyright")

        meta = vlayer.metadata()
        if lic is not None:
            meta.setLicenses([lic])
        if isinstance(cr, list):
            lst_txt = parse_copyright(cr)
            meta.setRights(lst_txt)
        vlayer.setMetadata(meta)

    def iter_layer(self):
        for lst in self.map_vlayer.values():
            for vlayer in lst:
                yield vlayer
    def has_layer(self, geom_str, idx):
        return (
            geom_str in self.map_vlayer and 
            idx < len(self.map_vlayer[geom_str])
            )
    def get_layer(self, geom_str, idx):
        return self.map_vlayer[geom_str][idx]
    def get_name(self):
        return self._group_name
    def _make_group_name(self, idx=None):
        """
        returns main layer group name
        """
        tags = "-(%s)" %(self.tags) if len(self.tags) else ""
        temp = "{title}-{id}{tags}" if idx is None else "{title}-{id}{tags}-{idx}"
        name = temp.format(
            id=self.meta.get("id",""),
            title=self.meta.get("title",""),
            tags=tags, idx=idx,
            )
        return name
    def _group_geom_name(self, geom_str):
        geom = QgsWkbTypes.geometryDisplayString(
            QgsWkbTypes.geometryType(
            QgsWkbTypes.parseType(
            geom_str))) if geom_str else self.NO_GEOM
        return geom 
    def _layer_name(self, geom_str, idx):
        """
        returns vlayer name shown in qgis
        """
        tags = "-(%s)" %(self.tags) if len(self.tags) else ""
        return "{title}-{id}{tags}-{geom}-{idx}".format(
            id=self.meta.get("id",""),
            title=self.meta.get("title",""),
            geom=geom_str, idx=idx, tags=tags,
            )
    def _db_layer_name(self, geom_str, idx):
        """
        returns name of the table corresponds to vlayer in sqlite db
        """
        return "{geom}_{idx}".format(geom=geom_str, idx=idx)
    def _layer_fname(self):
        """
        returns file name of the sqlite db corresponds to xyz layer
        """
        tags = self.tags.replace(",","_") if len(self.tags) else ""
        return "{id}_{tags}_{unique}".format(
            id=self.meta.get("id",""),
            tags=tags, unique=self.unique,
            )
    def get_id(self):
        return self.unique
    def get_map_fields(self):
        return self.map_fields
    def get_feat_cnt(self):
        cnt = 0
        for vlayer in self.iter_layer():
            cnt += get_feat_cnt_from_src(vlayer)
        return cnt
    def add_empty_group(self):
        tree_root = QgsProject.instance().layerTreeRoot()
        group = self.qgroups.get("main")
        if not group:
            name = self._make_group_name()
            all_names = [g.name() for g in tree_root.findGroups()]
            dupe_names = [x for x in all_names if x.startswith(name)]
            idx = len(dupe_names)
            if idx: name = self._make_group_name(idx)
            self._group_name = name
            group = tree_root.insertGroup(0, name)
            self.qgroups["main"] = group
            self._save_meta_node(group)
        return group
    def add_ext_layer(self, geom_str, idx):
        crs = QgsCoordinateReferenceSystem('EPSG:4326').toWkt()

        vlayer = self._init_ext_layer(geom_str, idx, crs)
        self._add_layer(geom_str, vlayer, idx)

        dom = QDomDocument()
        dom.setContent(LAYER_QML, True) # xyz_id non editable
        vlayer.importNamedStyle(dom)

        QgsProject.instance().addMapLayer(vlayer, False)

        group = self.add_empty_group()

        geom = self._group_geom_name(geom_str)
        order = self.GEOM_ORDER.get(geom)
        group_geom = self.qgroups.get(geom)
        group_geom = group_geom or (
            group.insertGroup(order,geom)
            if order is not None else 
            group.addGroup(geom)
        )
        self.qgroups[geom] = group_geom

        group_geom.addLayer(vlayer)
        
        if iface: iface.setActiveLayer(vlayer)
        return vlayer
    def _add_layer(self, geom_str, vlayer, idx):
        lst = self.map_vlayer.setdefault(geom_str, list())
        assert idx == len(lst), "vlayer count mismatch"
        lst.append(vlayer)

    def _init_ext_layer(self, geom_str, idx, crs):
        """ given non map of feat, init a qgis layer
        :map_feat: {geom_string: list_of_feat}
        """
        ext=self.ext
        driver_name = ext.upper() # might not needed for 

        layer_name = self._layer_name(geom_str, idx)
        
        # sqlite max connection 64
        # if xyz space -> more than 64 vlayer,
        # then create new fname
        
        # fname = make_unique_full_path(ext=ext)
        fname = make_fixed_full_path(self._layer_fname(),ext=ext)
        
        vlayer = QgsVectorLayer(
            "{geom}?crs={crs}&index=yes".format(geom=geom_str,crs=crs), 
            layer_name,"memory") # this should be done in main thread

        # QgsVectorFileWriter.writeAsVectorFormat(vlayer, fname, "UTF-8", vlayer.sourceCrs(), driver_name)

        db_layer_name = self._db_layer_name(geom_str, idx)

        options = QgsVectorFileWriter.SaveVectorOptions()
        options.fileEncoding = "UTF-8"
        options.driverName = driver_name
        options.ct = QgsCoordinateTransform(vlayer.sourceCrs(), vlayer.sourceCrs(), QgsProject.instance())
        options.layerName = db_layer_name
        options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteLayer # update mode
        err=QgsVectorFileWriter.writeAsVectorFormat(vlayer, fname, options)
        if err[0] == QgsVectorFileWriter.ErrCreateDataSource : 
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
            err=QgsVectorFileWriter.writeAsVectorFormat(vlayer, fname, options)
        if err[0] != QgsVectorFileWriter.NoError:
            raise Exception("%s: %s"%err)
        
        sql_constraint = '"%s" TEXT UNIQUE ON CONFLICT REPLACE'%(parser.QGS_XYZ_ID) # replace older duplicate
        # sql_constraint = '"%s" TEXT UNIQUE ON CONFLICT IGNORE'%(parser.QGS_XYZ_ID) # discard newer duplicate
        self._init_constraint(fname, sql_constraint, db_layer_name)

        uri = "%s|layername=%s"%(fname, db_layer_name)
        vlayer = QgsVectorLayer(uri, layer_name, "ogr")
        self._save_meta(vlayer)
        
        return vlayer

    def _init_constraint(self, fname, sql_constraint, layer_name):
        # https://sqlite.org/lang_altertable.html

        tmp_name = "tmp_" + layer_name

        conn = sqlite3.connect(fname)
        cur = conn.cursor()

        sql = 'SELECT type, sql FROM sqlite_master WHERE tbl_name="%s"'%(layer_name)
        cur.execute(sql)
        lst = cur.fetchall()
        if len(lst) == 0:
            raise Exception("No layer found in GPKG")
        lst_old_sql = [p[1] for p in lst]
        sql_create = lst_old_sql.pop(0)

        sql_create = sql_create.replace(layer_name, tmp_name)
        parts = sql_create.partition(")")
        sql_create = "".join([
            parts[0], 
            ", %s"%(sql_constraint), 
            parts[1], parts[2]
        ])
        # empty table, so insert is skipped
        lst_sql = [
            "PRAGMA foreign_keys = '0'",
            "BEGIN TRANSACTION",
            sql_create,
            "PRAGMA defer_foreign_keys = '1'",
            'DROP TABLE "%s"'%(layer_name),
            'ALTER TABLE "%s" RENAME TO "%s"'%(tmp_name, layer_name),
            "PRAGMA defer_foreign_keys = '0'"
        ] + lst_old_sql + [
            # 'PRAGMA "main".foreign_key_check', # does not return anything -> unused
            "COMMIT",
            "PRAGMA foreign_keys = '1'"
        ]
        for s in lst_sql:
            if "COMMIT" in s:
                conn.commit()
                continue
            # if "COMMIT" in s and not conn.in_transaction: continue

            cur.execute(s)

        conn.commit()
        conn.close()


""" Available vector format for QgsVectorFileWriter
[i.driverName for i in QgsVectorFileWriter.ogrDriverList()]
['GPKG', 'ESRI Shapefile', 'BNA',
'CSV', 'DGN', 'DXF', 'GML',
'GPX', 'GeoJSON', 'GeoRSS',
'Geoconcept', 'Interlis 1', 'Interlis 2',
'KML', 'MapInfo File', 'MapInfo MIF',
'ODS', 'S57', 'SQLite', 'SpatiaLite', 'XLSX']

[i.longName for i in QgsVectorFileWriter.ogrDriverList()]
['GeoPackage', 'ESRI Shapefile', 'Atlas BNA',
'Comma Separated Value [CSV]', 'Microstation DGN',
'AutoCAD DXF', 'Geography Markup Language [GML]',
'GPS eXchange Format [GPX]', 'GeoJSON', 'GeoRSS',
'Geoconcept', 'INTERLIS 1', 'INTERLIS 2',
'Keyhole Markup Language [KML]', 'Mapinfo TAB',
'Mapinfo MIF', 'Open Document Spreadsheet',
'S-57 Base file', 'SQLite', 'SpatiaLite',
'MS Office Open XML spreadsheet']
"""
