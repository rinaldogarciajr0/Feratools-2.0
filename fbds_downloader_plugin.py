import os
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
)
from qgis.core import Qgis, QgsFeature, QgsProject, QgsVectorLayer, QgsWkbTypes

from .exportar_dialog import ExportarDialog


PLUGIN_MENU = "&FeraTools"
SETTINGS_KEY = "feratools/fbds"
IDE_WFS_URL = "https://geoserver.meioambiente.mg.gov.br/ows"
SOURCE_FBDS = "FBDS"
SOURCE_IDE = "IDE-SISEMA"
DATA_TYPES = [
    ("Hidrografia - rios simples", "RIOS_SIMPLES", "Hidrografia"),
    ("Hidrografia - rios duplos", "RIOS_DUPLOS", "Rios_Duplos"),
    ("Nascentes", "NASCENTES", "Nascentes"),
]
IDE_BASINS = [
    ("Rio Grande", ["hidro", "grande"]),
    ("Rio Sao Francisco", ["hidro", "sao", "francisco"]),
    ("Rio Paraiba do Sul", ["hidro", "paraiba", "sul"]),
]
URL_TIMEOUT = 120


class FbdsDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.toolbar_action = None
        self.menu_action = None
        self.context_action = None
        self.settings = QSettings()
        self.icon_path = os.path.join(os.path.dirname(__file__), "icon_export.svg")

    def initGui(self):
        self.toolbar_action = QAction(
            QIcon(self.icon_path),
            "Extrair hidrografia",
            self.iface.mainWindow(),
        )
        self.toolbar_action.setToolTip("Extrair hidrografia da FBDS ou IDE-SISEMA")
        self.toolbar_action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.toolbar_action)

        self.menu_action = QAction(QIcon(self.icon_path), "Extrair hidrografia", self.iface.mainWindow())
        self.menu_action.triggered.connect(self.run)
        self.iface.addPluginToMenu(PLUGIN_MENU, self.menu_action)

        self.context_action = QAction(QIcon(self.icon_path), "Extrair hidrografia", self.iface.mainWindow())
        self.context_action.triggered.connect(self.run)
        self.iface.addCustomActionForLayerType(
            self.context_action,
            "FeraTools",
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
        dialog = FbdsDownloaderDialog(self.settings, self.iface, self.iface.mainWindow())
        dialog.exec_()


class FbdsDownloaderDialog(QDialog):
    def __init__(self, settings, iface, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.iface = iface
        self.setWindowTitle("Extrair hidrografia")
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.fbds_checkbox = QCheckBox(SOURCE_FBDS)
        self.fbds_checkbox.setChecked(True)
        self.ide_checkbox = QCheckBox(SOURCE_IDE)
        self.ide_checkbox.stateChanged.connect(self._update_ide_controls)
        form.addRow("Fonte", self.fbds_checkbox)
        form.addRow("", self.ide_checkbox)

        self.data_type_combo = QComboBox()
        for label, pattern, folder_name in DATA_TYPES:
            self.data_type_combo.addItem(label, {"pattern": pattern, "folder_name": folder_name})
        form.addRow("Dado", self.data_type_combo)

        self.uf_edit = QLineEdit()
        self.uf_edit.setMaxLength(2)
        self.uf_edit.setText(self.settings.value(f"{SETTINGS_KEY}/uf", ""))
        form.addRow("UF", self.uf_edit)

        self.municipios_edit = QPlainTextEdit()
        self.municipios_edit.setPlaceholderText("Exemplo: Mendes, Vassouras")
        self.municipios_edit.setFixedHeight(110)
        form.addRow("Municipios", self.municipios_edit)

        self.ide_basin_combo = QComboBox()
        for label, keywords in IDE_BASINS:
            self.ide_basin_combo.addItem(label, keywords)
        form.addRow("Hidrografia IDE-SISEMA", self.ide_basin_combo)
        self._update_ide_controls()

        layout.addLayout(form)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Extrair")
        buttons.accepted.connect(self._download)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_ide_controls(self):
        self.ide_basin_combo.setEnabled(self.ide_checkbox.isChecked())

    def _download(self):
        uf = self.uf_edit.text().strip().upper()
        municipios = self._parse_municipios(self.municipios_edit.toPlainText())
        sources = self._selected_sources()

        if not uf or len(uf) != 2:
            QMessageBox.warning(self, "FBDS", "Informe a UF com 2 letras.")
            return

        if SOURCE_FBDS in sources and not municipios:
            QMessageBox.warning(self, "FBDS", "Informe ao menos um municipio.")
            return

        if not sources:
            QMessageBox.warning(self, "FBDS", "Selecione ao menos uma fonte.")
            return

        data = self.data_type_combo.currentData()
        label = self.data_type_combo.currentText()
        base_folder = tempfile.mkdtemp(prefix=f"feratools_hidro_{data['folder_name']}_")
        os.makedirs(base_folder, exist_ok=True)

        self.settings.setValue(f"{SETTINGS_KEY}/uf", uf)
        self.progress.setValue(0)
        total_steps = self._total_steps(sources, municipios)

        results = []
        step_index = 0
        if SOURCE_FBDS in sources:
            for municipio in municipios:
                self.status_label.setText(f"Extraindo {municipio} ({SOURCE_FBDS})...")
                QApplication.processEvents()
                result = self._download_fbds_municipio(
                        uf,
                        municipio,
                        data["pattern"],
                        base_folder,
                        step_index,
                        total_steps,
                    )
                result["fonte"] = SOURCE_FBDS
                results.append(result)
                step_index += 1

        if SOURCE_IDE in sources:
            basin_label = self.ide_basin_combo.currentText()
            basin_keywords = self.ide_basin_combo.currentData()
            self.status_label.setText(f"Extraindo {basin_label} ({SOURCE_IDE})...")
            QApplication.processEvents()
            result = self._extract_ide_basin(
                    basin_label,
                    basin_keywords,
                    base_folder,
                    step_index,
                    total_steps,
                )
            result["fonte"] = SOURCE_IDE
            results.append(result)
            step_index += 1

        downloaded_paths = []
        for item in results:
            downloaded_paths.extend(item["paths"])

        action_text, added_layers = self._handle_downloaded_layers(downloaded_paths)
        try:
            shutil.rmtree(base_folder)
        except Exception:
            pass

        lines = [
            f"{item['fonte']} - {item['municipio']}: {item['status']} | arquivos: {item['baixados']} | erros: {item['erros']}"
            for item in results
        ]

        message = (
            f"Processo finalizado.\n\n"
            f"Fonte: {', '.join(sources)}\n"
            f"Tipo: {label}\n"
            f"UF: {uf}\n"
            f"Itens processados: {len(results)}\n\n"
            + "\n".join(lines)
            + f"\n\n{action_text}"
        )
        self.status_label.setText("Processo finalizado.")
        QMessageBox.information(self, "FBDS", message)

    def _selected_sources(self):
        sources = []
        if self.fbds_checkbox.isChecked():
            sources.append(SOURCE_FBDS)
        if self.ide_checkbox.isChecked():
            sources.append(SOURCE_IDE)
        return sources

    def _total_steps(self, sources, municipios):
        total = 0
        if SOURCE_FBDS in sources:
            total += len(municipios)
        if SOURCE_IDE in sources:
            total += 1
        return max(total, 1)

    def _download_fbds_municipio(self, uf, municipio_digitado, pattern, base_folder, municipio_index, total_municipios):
        municipio = self._normalize_municipio(municipio_digitado)
        url_base = f"https://geo.fbds.org.br/{uf}/{municipio}/HIDROGRAFIA/"
        output_folder = os.path.join(base_folder, municipio)
        os.makedirs(output_folder, exist_ok=True)
        self._set_progress(municipio_index, total_municipios, 5)

        try:
            self.status_label.setText(f"Acessando FBDS: {municipio_digitado}...")
            QApplication.processEvents()
            content = self._read_url(url_base, URL_TIMEOUT).decode("utf-8", errors="ignore")
        except Exception:
            self._set_progress(municipio_index, total_municipios, 100)
            return self._result(municipio_digitado, municipio, "Erro ao acessar", 0, 1, 0, output_folder, url_base)

        self._set_progress(municipio_index, total_municipios, 20)
        links = sorted(set(re.findall(r'href="([^"]*' + re.escape(pattern) + r'[^"]*)"', content)))
        if not links:
            self._set_progress(municipio_index, total_municipios, 100)
            return self._result(municipio_digitado, municipio, "Nenhum arquivo encontrado", 0, 0, 0, output_folder, url_base)

        downloaded = 0
        errors = 0
        downloaded_paths = []
        for link in links:
            try:
                file_url = urljoin(url_base, link)
                file_name = unquote(os.path.basename(urlparse(file_url).path))
                output_path = os.path.join(output_folder, file_name)
                self.status_label.setText(f"Baixando {file_name}...")
                QApplication.processEvents()
                with open(output_path, "wb") as output_file:
                    output_file.write(self._read_url(file_url, URL_TIMEOUT))
                downloaded += 1
                downloaded_paths.append(output_path)
            except Exception:
                errors += 1
            file_step = 20 + int((downloaded + errors) / len(links) * 60)
            self._set_progress(municipio_index, total_municipios, file_step)

        self.status_label.setText(f"Preparando camadas de {municipio_digitado}...")
        QApplication.processEvents()
        self._set_progress(municipio_index, total_municipios, 100)
        status = "Concluido" if errors == 0 else "Parcial"
        return self._result(
            municipio_digitado,
            municipio,
            status,
            downloaded,
            errors,
            0,
            output_folder,
            url_base,
            self._shapefile_paths(downloaded_paths, pattern),
        )

    def _extract_ide_basin(self, basin_label, basin_keywords, base_folder, municipio_index, total_municipios):
        try:
            self.status_label.setText("Consultando camadas da IDE-SISEMA...")
            QApplication.processEvents()
            capabilities = self._ide_capabilities()
            hydro_type = self._find_feature_type(capabilities, basin_keywords)
            self._set_progress(municipio_index, total_municipios, 15)

            hydro_layer = self._download_ide_feature_type(
                hydro_type["name"],
                os.path.join(base_folder, f"ide_hidrografia_{municipio_index}.geojson"),
            )
            self._set_progress(municipio_index, total_municipios, 70)

            memory_layer = self._copy_to_memory_layer(hydro_layer)
            if memory_layer is None or memory_layer.featureCount() == 0:
                self._set_progress(municipio_index, total_municipios, 100)
                return self._result(basin_label, "", "Nenhuma feicao encontrada", 0, 0)

            memory_layer.setName(f"IDE_SISEMA_{self._normalize_municipio(basin_label)}")
            memory_path = self._register_prepared_layer(memory_layer)
            self._set_progress(municipio_index, total_municipios, 100)
            return self._result(basin_label, "", "Concluido", memory_layer.featureCount(), 0, 0, "", IDE_WFS_URL, [memory_path])
        except Exception as exc:
            self._set_progress(municipio_index, total_municipios, 100)
            return self._result(basin_label, "", f"Erro ao acessar: {str(exc)}", 0, 1)

    def _shapefile_paths(self, paths, pattern):
        shapefiles = []
        for path in sorted(paths):
            file_name = os.path.basename(path)
            if file_name.lower().endswith(".shp") and pattern in file_name.upper():
                shapefiles.append(path)
        return shapefiles

    def _add_downloaded_layers(self, paths):
        layers = self._temporary_layers_from_paths(paths)
        for layer in layers:
            QgsProject.instance().addMapLayer(layer)
        return layers

    def _set_progress(self, municipio_index, total_municipios, local_percent):
        if total_municipios <= 0:
            self.progress.setValue(0)
            return
        value = int(((municipio_index + (local_percent / 100.0)) / total_municipios) * 100)
        self.progress.setValue(max(0, min(100, value)))
        QApplication.processEvents()

    def _handle_downloaded_layers(self, paths):
        if not paths:
            return "Nenhuma camada foi encontrada para adicionar ou exportar.", 0

        layers = self._temporary_layers_from_paths(paths)
        if not layers:
            return "Nenhuma camada valida foi criada.", 0

        answer = QMessageBox.question(
            self,
            "Exportar",
            "Deseja exportar as camadas baixadas pelo FeraTools?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if answer != QMessageBox.Yes:
            for layer in layers:
                QgsProject.instance().addMapLayer(layer)
            return f"Camadas temporarias adicionadas ao QGIS: {len(layers)}", len(layers)

        exported = 0
        for layer in layers:
            dialog = ExportarDialog(layer, self)
            if dialog.exec_() == dialog.Accepted:
                exported += 1
        return f"Camadas enviadas para exportacao: {exported}", exported

    def _temporary_layers_from_paths(self, paths):
        layers = []
        for path in sorted(paths):
            if path.startswith("memory:"):
                memory_layer = self._prepared_memory_layers.get(path)
            else:
                source = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
                if not source.isValid():
                    continue
                memory_layer = self._copy_to_memory_layer(source)
            if memory_layer and memory_layer.isValid():
                layers.append(memory_layer)
        return layers

    @property
    def _prepared_memory_layers(self):
        if not hasattr(self, "_prepared_layers"):
            self._prepared_layers = {}
        return self._prepared_layers

    def _register_prepared_layer(self, layer):
        key = f"memory:{id(layer)}"
        self._prepared_memory_layers[key] = layer
        return key

    def _copy_to_memory_layer(self, source):
        geometry_name = QgsWkbTypes.displayString(source.wkbType())
        crs = source.crs().authid()
        uri = geometry_name
        if crs:
            uri += f"?crs={crs}"

        memory_layer = QgsVectorLayer(uri, source.name(), "memory")
        if not memory_layer.isValid():
            return None

        provider = memory_layer.dataProvider()
        provider.addAttributes(source.fields())
        memory_layer.updateFields()

        features = []
        for source_feature in source.getFeatures():
            feature = QgsFeature(memory_layer.fields())
            feature.setGeometry(source_feature.geometry())
            feature.setAttributes(source_feature.attributes())
            features.append(feature)

        if features:
            provider.addFeatures(features)
        memory_layer.updateExtents()
        return memory_layer

    def _ide_capabilities(self):
        last_error = None
        for version in ("1.0.0", "1.1.0", "2.0.0"):
            try:
                params = urlencode({"service": "WFS", "version": version, "request": "GetCapabilities"})
                root = ElementTree.fromstring(self._read_url(f"{IDE_WFS_URL}?{params}", URL_TIMEOUT))
                feature_types = self._parse_capabilities_feature_types(root)
                if feature_types:
                    return feature_types
            except Exception as exc:
                last_error = exc
        raise Exception(f"nao foi possivel ler o catalogo WFS ({last_error})")

    def _parse_capabilities_feature_types(self, root):
        feature_types = []
        for feature_type in root.iter():
            if self._xml_name(feature_type.tag) != "FeatureType":
                continue

            name = ""
            title = ""
            for child in feature_type:
                child_name = self._xml_name(child.tag)
                if child_name == "Name":
                    name = child.text or ""
                elif child_name == "Title":
                    title = child.text or ""
            if name:
                feature_types.append({"name": name, "title": title})
        return feature_types

    def _xml_name(self, tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def _find_feature_type(self, feature_types, keywords):
        normalized_keywords = [self._normalize_search_text(keyword) for keyword in keywords]
        for feature_type in feature_types:
            haystack = self._normalize_search_text(f"{feature_type['name']} {feature_type['title']}")
            if all(keyword in haystack for keyword in normalized_keywords):
                return feature_type
        terms = ", ".join(keywords)
        raise Exception(f"camada WFS nao encontrada na IDE-SISEMA ({terms})")

    def _download_ide_feature_type(self, type_name, output_path, bbox=None):
        params = {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": type_name,
            "outputFormat": "application/json",
            "srsName": "EPSG:4674",
        }
        if bbox is not None:
            params["bbox"] = f"{bbox.xMinimum()},{bbox.yMinimum()},{bbox.xMaximum()},{bbox.yMaximum()},EPSG:4674"

        url = f"{IDE_WFS_URL}?{urlencode(params, quote_via=quote)}"
        with open(output_path, "wb") as output_file:
            output_file.write(self._read_url(url, URL_TIMEOUT))

        layer = QgsVectorLayer(output_path, os.path.splitext(os.path.basename(output_path))[0], "ogr")
        if not layer.isValid():
            raise Exception("Nao foi possivel carregar a camada baixada da IDE-SISEMA.")
        return layer

    def _read_url(self, url, timeout):
        request = Request(
            url,
            headers={
                "User-Agent": "FeraTools QGIS",
                "Accept": "*/*",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return response.read()

    def _normalize_search_text(self, text):
        normalized = unicodedata.normalize("NFKD", str(text))
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", normalized.lower())

    def _parse_municipios(self, text):
        values = [item.strip() for item in re.split(r"[,;|\r\n]+", text) if item.strip()]
        unique = []
        seen = set()
        for value in values:
            key = value.upper()
            if key in seen:
                continue
            seen.add(key)
            unique.append(value)
        return unique

    def _normalize_municipio(self, municipio):
        text = unicodedata.normalize("NFKD", municipio)
        text = "".join(char for char in text if not unicodedata.combining(char))
        text = text.strip().upper()
        text = re.sub(r"[^A-Z0-9]+", "_", text)
        return text.strip("_")

    def _result(self, municipio, normalized, status, downloaded, errors, layers_added=0, folder="", url="", paths=None):
        return {
            "municipio": municipio,
            "municipio_normalizado": normalized,
            "status": status,
            "baixados": downloaded,
            "erros": errors,
            "camadas": layers_added,
            "paths": paths or [],
            "pasta": folder,
            "url": url,
        }
