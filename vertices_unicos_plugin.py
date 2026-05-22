import os

from qgis.PyQt.QtCore import QMetaType, QSettings
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .dialogs import FieldConfig
from .pegar_coordenadas_plugin import SETTINGS_KEY


PLUGIN_MENU = "&FeraTools"
CONTEXT_MENU = "FeraTools"


class VerticesUnicosPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.toolbar_action = None
        self.menu_action = None
        self.context_action = None
        self.settings = QSettings()
        self.icon_path = os.path.join(os.path.dirname(__file__), "icon_coord.svg")

    def initGui(self):
        self.toolbar_action = QAction(
            QIcon(self.icon_path),
            "Extrair vértices",
            self.iface.mainWindow(),
        )
        self.toolbar_action.setToolTip("Extrair vértices únicos com coordenadas")
        self.toolbar_action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.toolbar_action)

        self.menu_action = QAction(QIcon(self.icon_path), "Extrair vértices", self.iface.mainWindow())
        self.menu_action.triggered.connect(self.run)
        self.iface.addPluginToMenu(PLUGIN_MENU, self.menu_action)

        self.context_action = QAction(QIcon(self.icon_path), "Extrair vértices", self.iface.mainWindow())
        self.context_action.triggered.connect(self.run)
        self.iface.addCustomActionForLayerType(
            self.context_action,
            CONTEXT_MENU,
            Qgis.LayerType.Vector,
            True,
        )

    def unload(self):
        if self.toolbar_action:
            self.iface.removeToolBarIcon(self.toolbar_action)
            self.toolbar_action = None

        if self.menu_action:
            self.iface.removePluginMenu(PLUGIN_MENU, self.menu_action)
            self.menu_action = None

        if self.context_action:
            try:
                self.iface.removeCustomActionForLayerType(self.context_action)
            except Exception:
                pass
            self.context_action = None

    def run(self):
        layer = self.iface.activeLayer()
        if not layer or layer.type() != QgsMapLayerType.VectorLayer:
            self._warn("Selecione uma camada vetorial primeiro.")
            return

        if layer.geometryType() == QgsWkbTypes.NullGeometry:
            self._warn("A camada selecionada nao possui geometria.")
            return

        if not layer.crs().isValid():
            self._warn("A camada ativa nao possui SRC/CRS valido.")
            return

        try:
            output = self._create_vertices_layer(layer, self._default_config("X"), self._default_config("Y"))
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Extrair vértices", str(exc))
            return

        QgsProject.instance().addMapLayer(output)
        self.iface.setActiveLayer(output)
        self.iface.showAttributeTable(output)
        self.iface.messageBar().pushSuccess(
            "Extrair vértices",
            f"Camada criada com {output.featureCount()} vertice(s).",
        )

    def _create_vertices_layer(self, source_layer, x_cfg, y_cfg):
        same_field = x_cfg.field_name.lower() == y_cfg.field_name.lower()
        if same_field:
            raise Exception("Os campos X e Y precisam ter nomes diferentes.")

        target_crs_x = QgsCoordinateReferenceSystem(x_cfg.target_authid)
        target_crs_y = QgsCoordinateReferenceSystem(y_cfg.target_authid)
        if not target_crs_x.isValid() or not target_crs_y.isValid():
            raise Exception("Um dos CRS selecionados e invalido.")

        selected_only = x_cfg.selected_only or y_cfg.selected_only
        if selected_only and source_layer.selectedFeatureCount() == 0:
            raise Exception("Nao ha feicoes selecionadas para processar.")

        output = QgsVectorLayer("Point", f"Vertices unicos - {source_layer.name()}", "memory")
        output.setCrs(source_layer.crs())
        provider = output.dataProvider()
        provider.addAttributes(
            [
                QgsField("vertice", QMetaType.Type.Int, "integer", 10, 0),
                self._coordinate_field(x_cfg),
                self._coordinate_field(y_cfg),
            ]
        )
        output.updateFields()

        request = QgsFeatureRequest()
        if selected_only:
            request.setFilterFids([feature.id() for feature in source_layer.selectedFeatures()])

        ct_x = QgsCoordinateTransform(source_layer.crs(), target_crs_x, QgsProject.instance())
        ct_y = QgsCoordinateTransform(source_layer.crs(), target_crs_y, QgsProject.instance())

        features = []
        for source_feature in source_layer.getFeatures(request):
            for vertex_index, point in enumerate(self._unique_vertices(source_feature.geometry()), start=1):
                point_xy = QgsPointXY(point.x(), point.y())
                feature = QgsFeature(output.fields())
                feature.setGeometry(QgsGeometry.fromPointXY(point_xy))
                transformed_x = ct_x.transform(point_xy)
                transformed_y = ct_y.transform(point_xy)
                feature.setAttributes(
                    [
                        vertex_index,
                        self._format_axis_value(transformed_x.x(), x_cfg, is_x=True),
                        self._format_axis_value(transformed_y.y(), y_cfg, is_x=False),
                    ]
                )
                features.append(feature)

        if not features:
            raise Exception("Nenhum vertice foi criado a partir da camada selecionada.")

        provider.addFeatures(features)
        output.updateExtents()
        return output

    def _default_config(self, axis):
        axis = axis.upper()
        field_key = "x_field_name" if axis == "X" else "y_field_name"
        default_field = "coord_x" if axis == "X" else "coord_y"
        return FieldConfig(
            axis=axis,
            field_name=self._load_setting(field_key, default_field),
            output_format=self._load_setting("output_format", "decimal"),
            precision=int(self._load_setting("precision", 3)),
            target_authid=self._load_setting("target_authid", "EPSG:31983"),
            selected_only=False,
        )

    def _unique_vertices(self, geometry):
        if not geometry or geometry.isEmpty():
            return []

        vertices = []
        seen = set()
        for point in geometry.vertices():
            key = (point.x(), point.y())
            if key in seen:
                continue
            seen.add(key)
            vertices.append(point)
        return vertices

    def _coordinate_field(self, cfg):
        if cfg.output_format == "decimal":
            return QgsField(cfg.field_name, QMetaType.Type.Double, "double", 20, cfg.precision)
        return QgsField(cfg.field_name, QMetaType.Type.QString, "string", 40, 0)

    def _format_axis_value(self, value, cfg, is_x):
        if cfg.output_format == "decimal":
            return round(value, cfg.precision)
        return self._decimal_to_dms(value, cfg.precision, is_longitude=is_x)

    def _decimal_to_dms(self, decimal_value, precision, is_longitude):
        hemi = self._hemisphere(decimal_value, is_longitude)
        absolute = abs(decimal_value)
        degrees = int(absolute)
        minutes_full = (absolute - degrees) * 60.0
        minutes = int(minutes_full)
        seconds = round((minutes_full - minutes) * 60.0, precision)

        if seconds >= 60:
            seconds = 0.0
            minutes += 1
        if minutes >= 60:
            minutes = 0
            degrees += 1

        sec_text = f"{seconds:.{precision}f}"
        return f"{degrees} deg {minutes:02d}' {sec_text}\" {hemi}"

    def _hemisphere(self, value, is_longitude):
        if is_longitude:
            return "E" if value >= 0 else "W"
        return "N" if value >= 0 else "S"

    def _load_setting(self, key, default):
        return self.settings.value(f"{SETTINGS_KEY}/{key}", default)

    def _save_defaults(self, x_cfg, y_cfg):
        self.settings.setValue(f"{SETTINGS_KEY}/x_field_name", x_cfg.field_name)
        self.settings.setValue(f"{SETTINGS_KEY}/y_field_name", y_cfg.field_name)
        self.settings.setValue(f"{SETTINGS_KEY}/output_format", y_cfg.output_format)
        self.settings.setValue(f"{SETTINGS_KEY}/precision", y_cfg.precision)
        self.settings.setValue(f"{SETTINGS_KEY}/target_authid", y_cfg.target_authid)

    def _warn(self, message):
        QMessageBox.warning(self.iface.mainWindow(), "Extrair vértices", message)
        self.iface.messageBar().pushWarning("Extrair vértices", message)
