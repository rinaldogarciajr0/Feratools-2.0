import os

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QVBoxLayout,
)
from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)


PLUGIN_MENU = "&FeraTools"
CONTEXT_MENU = "FeraTools"
APP_WATERCOURSE = "Curso de agua"
APP_SPRING = "Nascente"
WIDTH_RANGES = [
    ("0 a 10 m", 30),
    ("10 a 50 m", 50),
    ("50 a 200 m", 100),
    ("200 a 600 m", 200),
    ("Acima de 600 m", 500),
]


class CriarAppsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.toolbar_action = None
        self.menu_action = None
        self.context_action = None
        self.icon_path = os.path.join(os.path.dirname(__file__), "icon_area.svg")

    def initGui(self):
        self.toolbar_action = QAction(QIcon(self.icon_path), "Criar APPs", self.iface.mainWindow())
        self.toolbar_action.setToolTip("Criar APPs conforme a Lei 12.651/2012")
        self.toolbar_action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.toolbar_action)

        self.menu_action = QAction(QIcon(self.icon_path), "Criar APPs", self.iface.mainWindow())
        self.menu_action.triggered.connect(self.run)
        self.iface.addPluginToMenu(PLUGIN_MENU, self.menu_action)

        self.context_action = QAction(QIcon(self.icon_path), "Criar APPs", self.iface.mainWindow())
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

        if not layer.crs().isValid():
            self._warn("A camada ativa nao possui SRC/CRS valido.")
            return

        if layer.crs().isGeographic():
            self._warn("Use uma camada em CRS projetado em metros, como UTM.")
            return

        dialog = CriarAppsDialog(layer.selectedFeatureCount() > 0, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return

        values = dialog.values()
        try:
            output = self._create_app_layer(layer, values)
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Criar APPs", str(exc))
            return

        QgsProject.instance().addMapLayer(output)
        self.iface.setActiveLayer(output)
        self.iface.messageBar().pushSuccess(
            "Criar APPs",
            f"Camada criada com {output.featureCount()} APP(s).",
        )

    def _create_app_layer(self, source_layer, values):
        app_type = values["app_type"]
        distance = values["distance"]
        selected_only = values["selected_only"]

        if selected_only and source_layer.selectedFeatureCount() == 0:
            raise Exception("Nao ha feicoes selecionadas para processar.")

        if app_type == APP_SPRING and source_layer.geometryType() != QgsWkbTypes.PointGeometry:
            raise Exception("Para nascente, use uma camada de pontos.")

        if app_type == APP_WATERCOURSE and source_layer.geometryType() not in (
            QgsWkbTypes.LineGeometry,
            QgsWkbTypes.PolygonGeometry,
        ):
            raise Exception("Para curso de agua, use uma camada de linhas ou poligonos.")

        output = QgsVectorLayer("Polygon?crs=" + source_layer.crs().authid(), "APPs", "memory")
        provider = output.dataProvider()
        provider.addAttributes(
            [
                QgsField("tipo_app", QVariant.String, "string", 40, 0),
                QgsField("largura_rio", QVariant.String, "string", 40, 0),
                QgsField("dist_app_m", QVariant.Double, "double", 10, 2),
            ]
        )
        output.updateFields()

        request = QgsFeatureRequest()
        if selected_only:
            request.setFilterFids(source_layer.selectedFeatureIds())

        features = []
        for source_feature in source_layer.getFeatures(request):
            geometry = source_feature.geometry()
            if not geometry or geometry.isEmpty():
                continue

            buffered = geometry.buffer(distance, 24)
            if not buffered or buffered.isEmpty():
                continue

            feature = QgsFeature(output.fields())
            feature.setGeometry(buffered)
            feature.setAttributes([app_type, values["width_label"], float(distance)])
            features.append(feature)

        if not features:
            raise Exception("Nenhuma APP foi criada.")

        provider.addFeatures(features)
        output.updateExtents()
        return output

    def _warn(self, message):
        QMessageBox.warning(self.iface.mainWindow(), "Criar APPs", message)
        self.iface.messageBar().pushWarning("Criar APPs", message)


class CriarAppsDialog(QDialog):
    def __init__(self, has_selection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Criar APPs")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.app_type_combo = QComboBox()
        self.app_type_combo.addItem(APP_WATERCOURSE, APP_WATERCOURSE)
        self.app_type_combo.addItem(APP_SPRING, APP_SPRING)
        self.app_type_combo.currentIndexChanged.connect(self._update_controls)
        form.addRow("Tipo", self.app_type_combo)

        self.width_combo = QComboBox()
        for label, distance in WIDTH_RANGES:
            self.width_combo.addItem(label, {"label": label, "distance": distance})
        form.addRow("Largura do curso", self.width_combo)

        self.selected_only_checkbox = QCheckBox("Apenas feicoes selecionadas")
        self.selected_only_checkbox.setEnabled(has_selection)
        form.addRow("Escopo", self.selected_only_checkbox)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_controls()

    def _update_controls(self):
        self.width_combo.setEnabled(self.app_type_combo.currentData() == APP_WATERCOURSE)

    def values(self):
        app_type = self.app_type_combo.currentData()
        if app_type == APP_SPRING:
            return {
                "app_type": app_type,
                "width_label": "Nascente",
                "distance": 50,
                "selected_only": self.selected_only_checkbox.isChecked(),
            }

        width_data = self.width_combo.currentData()
        return {
            "app_type": app_type,
            "width_label": width_data["label"],
            "distance": width_data["distance"],
            "selected_only": self.selected_only_checkbox.isChecked(),
        }
