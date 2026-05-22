import os
import random
import re
import unicodedata
import zipfile
from xml.etree import ElementTree

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox
from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMapLayerType,
    QgsPointXY,
    QgsProject,
    QgsRendererCategory,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsMarkerSymbol,
    QgsSvgMarkerSymbolLayer,
)

from .linhas_plantio_dialog import LinhasPlantioDialog


PLUGIN_MENU = "&FeraTools"
CONTEXT_MENU = "FeraTools"
VALID_GROUPS = {"Diversidade", "Cobertura"}
SPECIES_FIELD_NAME = "Especie"
SEQUENCE_FIELD_NAME = "Numero"
TREE_ICONS = {
    "Diversidade": "tree_diversidade.svg",
    "Cobertura": "tree_cobertura.svg",
}


class LinhasPlantioPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.toolbar_action = None
        self.menu_action = None
        self.context_action = None
        self.icon_path = os.path.join(os.path.dirname(__file__), "icon_linhas_plantio.svg")
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        self.toolbar_action = QAction(
            QIcon(self.icon_path),
            "Linhas de Plantio",
            self.iface.mainWindow(),
        )
        self.toolbar_action.setToolTip("Criar pontos de plantio recortados pela camada ativa")
        self.toolbar_action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.toolbar_action)

        self.menu_action = QAction(
            QIcon(self.icon_path),
            "Linhas de Plantio",
            self.iface.mainWindow(),
        )
        self.menu_action.triggered.connect(self.run)
        self.iface.addPluginToMenu(PLUGIN_MENU, self.menu_action)

        self.context_action = QAction(
            QIcon(self.icon_path),
            "Linhas de Plantio",
            self.iface.mainWindow(),
        )
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

        if not layer:
            self._warn("Selecione uma camada poligonal primeiro.")
            return

        if layer.type() != QgsMapLayerType.VectorLayer:
            self._warn("A camada ativa nao e vetorial.")
            return

        if layer.geometryType() != QgsWkbTypes.PolygonGeometry:
            self._warn("A camada ativa precisa ser poligonal para recortar a grade.")
            return

        if not layer.crs().isValid():
            self._warn("A camada ativa nao possui SRC/CRS valido.")
            return

        if layer.crs().isGeographic():
            self._warn(
                "A camada esta em coordenadas geograficas. Use uma camada em UTM ou outro SRC em metros."
            )
            return

        dialog = LinhasPlantioDialog(layer, self.iface.mainWindow())
        if dialog.exec_() != dialog.Accepted:
            return

        try:
            output = self._create_grid_layer(layer, dialog.values())
        except Exception as exc:
            QMessageBox.critical(self.iface.mainWindow(), "Linhas de Plantio", str(exc))
            return

        QgsProject.instance().addMapLayer(output)
        self.iface.setActiveLayer(output)
        self.iface.messageBar().pushSuccess(
            "Linhas de Plantio",
            f"Camada criada com {output.featureCount()} ponto(s).",
        )

    def _create_grid_layer(self, source_layer, config):
        clip_geometry = self._layer_union_geometry(source_layer)
        if clip_geometry is None or clip_geometry.isEmpty():
            raise Exception("Nao foi possivel montar a geometria de recorte da camada.")

        output = QgsVectorLayer("Point", "Linhas de Plantio", "memory")
        output.setCrs(source_layer.crs())
        provider = output.dataProvider()
        provider.addAttributes(
            [
                QgsField(SEQUENCE_FIELD_NAME, QVariant.Int, "integer", 10, 0),
                QgsField(
                    config.field_name,
                    config.field_type,
                    config.field_type_name,
                    config.field_length,
                    config.field_precision,
                ),
                QgsField(SPECIES_FIELD_NAME, QVariant.String, "string", 80, 0),
            ]
        )
        output.updateFields()

        expression = QgsExpression(config.expression)
        if expression.hasParserError():
            raise Exception(f"Expressao invalida: {expression.parserErrorString()}")

        point_features = self._build_point_features(source_layer, clip_geometry, output.fields(), config)
        if not point_features:
            raise Exception("Nenhum ponto foi criado dentro da camada selecionada.")

        provider.addFeatures(point_features)
        output.updateExtents()

        species_by_group = self._read_species_file(config.species_file) if config.species_file else None
        removed = self._calculate_group_field(output, config, expression, species_by_group)
        self._apply_categorized_style(output, config.field_name)

        if removed:
            output.setName(f"Linhas de Plantio ({output.featureCount()} pontos)")

        return output

    def _layer_union_geometry(self, layer):
        geometries = []
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            if geometry and not geometry.isEmpty():
                geometries.append(QgsGeometry(geometry))

        if not geometries:
            return None

        if len(geometries) == 1:
            return geometries[0]

        return QgsGeometry.unaryUnion(geometries)

    def _build_point_features(self, source_layer, clip_geometry, fields, config):
        extent = source_layer.extent()
        features = []
        y = extent.yMinimum()

        while y <= extent.yMaximum() + 0.000001:
            x = extent.xMinimum()
            while x <= extent.xMaximum() + 0.000001:
                point = QgsPointXY(x, y)
                point_geometry = QgsGeometry.fromPointXY(point)
                if clip_geometry.intersects(point_geometry):
                    feature = QgsFeature(fields)
                    feature.setGeometry(point_geometry)
                    feature.setAttributes([None] * fields.count())
                    features.append(feature)
                x += config.horizontal_spacing
            y += config.vertical_spacing

        return features

    def _calculate_group_field(self, layer, config, expression, species_by_group=None):
        field_idx = layer.fields().indexFromName(config.field_name)
        if field_idx < 0:
            raise Exception(f"Campo '{config.field_name}' nao foi encontrado na camada criada.")

        sequence_idx = layer.fields().indexFromName(SEQUENCE_FIELD_NAME)
        species_idx = layer.fields().indexFromName(SPECIES_FIELD_NAME)
        if sequence_idx < 0 or species_idx < 0:
            raise Exception("Campos Numero e Especie nao foram encontrados na camada criada.")

        context = QgsExpressionContext()
        context.appendScopes(QgsExpressionContextUtils.globalProjectLayerScopes(layer))

        layer.startEditing()
        ids_to_delete = []
        group_features = {"Cobertura": [], "Diversidade": []}
        for feature in layer.getFeatures():
            context.setFeature(feature)
            value = expression.evaluate(context)
            if expression.hasEvalError():
                layer.rollBack()
                raise Exception(f"Erro ao avaliar expressao: {expression.evalErrorString()}")

            if str(value) not in VALID_GROUPS:
                ids_to_delete.append(feature.id())
                continue

            layer.changeAttributeValue(feature.id(), field_idx, value)
            group_features[str(value)].append(feature.id())

        if ids_to_delete:
            layer.deleteFeatures(ids_to_delete)

        if species_by_group:
            species_values = self._distribute_species(layer, group_features, species_by_group)
        else:
            species_values = {}

        sequence = 1
        for feature in layer.getFeatures():
            layer.changeAttributeValue(feature.id(), sequence_idx, sequence)
            if feature.id() in species_values:
                layer.changeAttributeValue(feature.id(), species_idx, species_values[feature.id()])
            sequence += 1

        if not layer.commitChanges():
            errors = "\n".join(layer.commitErrors()) or "Falha ao salvar atributos da camada criada."
            raise Exception(errors)

        return len(ids_to_delete)

    def _apply_categorized_style(self, layer, field_name):
        categories = [
            self._category("Diversidade", "#FF002D"),
            self._category("Cobertura", "#00FF07"),
        ]
        renderer = QgsCategorizedSymbolRenderer(field_name, categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _category(self, value, color):
        symbol = QgsMarkerSymbol()
        icon_path = os.path.join(self.plugin_dir, TREE_ICONS[value])
        svg_layer = QgsSvgMarkerSymbolLayer(icon_path, 4.0, 0.0)
        svg_layer.setFillColor(QColor(color))
        svg_layer.setStrokeColor(QColor("#232323"))
        svg_layer.setStrokeWidth(0.15)
        symbol.changeSymbolLayer(0, svg_layer)
        return QgsRendererCategory(value, symbol, value)

    def _read_species_file(self, path):
        if not os.path.exists(path):
            raise Exception("A planilha de especies nao foi encontrada.")

        rows = self._read_xlsx_rows(path)
        if len(rows) < 2:
            raise Exception("A planilha de especies precisa ter cabecalho e ao menos uma especie.")

        headers = [self._normalize_header(value) for value in rows[0]]
        species_col = self._find_column(headers, ("especie", "species"))
        amount_col = self._find_column(headers, ("quantidade", "qtd", "mudas", "amount", "quantity"))
        class_col = self._find_column(headers, ("classe", "classificacao", "grupo", "tipo", "categoria"))

        species_by_group = {"Cobertura": [], "Diversidade": []}
        for row in rows[1:]:
            species = self._cell(row, species_col).strip()
            amount_text = self._cell(row, amount_col).strip()
            species_class = self._cell(row, class_col).strip().upper()
            if not species and not amount_text and not species_class:
                continue

            if not species or not amount_text or not species_class:
                raise Exception("Cada linha da planilha precisa ter especie, quantidade e classificacao.")

            try:
                amount = int(float(amount_text.replace(",", ".")))
            except ValueError:
                raise Exception(f"Quantidade invalida para a especie '{species}'.")

            if amount < 0:
                raise Exception(f"Quantidade negativa para a especie '{species}'.")

            if species_class == "PI":
                group = "Cobertura"
            elif species_class in {"SI", "ST", "CL"}:
                group = "Diversidade"
            else:
                raise Exception(f"Classificacao invalida para a especie '{species}'. Use PI, SI, ST ou CL.")

            species_by_group[group].extend([species] * amount)

        if not species_by_group["Cobertura"] and not species_by_group["Diversidade"]:
            raise Exception("A planilha nao possui especies validas.")

        return species_by_group

    def _read_xlsx_rows(self, path):
        with zipfile.ZipFile(path) as workbook:
            shared_strings = self._read_shared_strings(workbook)
            sheet_path = self._first_sheet_path(workbook)
            root = ElementTree.fromstring(workbook.read(sheet_path))

        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rows = []
        for row_node in root.findall(".//x:sheetData/x:row", namespace):
            cells = {}
            max_col = -1
            for cell_node in row_node.findall("x:c", namespace):
                reference = cell_node.attrib.get("r", "")
                col_idx = self._column_index(reference)
                if col_idx < 0:
                    continue
                cells[col_idx] = self._xlsx_cell_value(cell_node, shared_strings, namespace)
                max_col = max(max_col, col_idx)
            if max_col >= 0:
                rows.append([cells.get(index, "") for index in range(max_col + 1)])

        return rows

    def _read_shared_strings(self, workbook):
        try:
            root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
        except KeyError:
            return []

        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        strings = []
        for item in root.findall("x:si", namespace):
            texts = [node.text or "" for node in item.findall(".//x:t", namespace)]
            strings.append("".join(texts))
        return strings

    def _first_sheet_path(self, workbook):
        root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        workbook_ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
        first_sheet = root.find("x:sheets/x:sheet", workbook_ns)
        if first_sheet is None:
            raise Exception("A planilha nao possui abas.")

        relation_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        for relation in rels.findall("r:Relationship", rel_ns):
            if relation.attrib.get("Id") == relation_id:
                target = relation.attrib.get("Target", "")
                if target.startswith("/"):
                    return target.lstrip("/")
                return "xl/" + target.lstrip("/")

        raise Exception("Nao foi possivel localizar a primeira aba da planilha.")

    def _xlsx_cell_value(self, cell_node, shared_strings, namespace):
        cell_type = cell_node.attrib.get("t")
        if cell_type == "inlineStr":
            texts = [node.text or "" for node in cell_node.findall(".//x:t", namespace)]
            return "".join(texts)

        value_node = cell_node.find("x:v", namespace)
        if value_node is None or value_node.text is None:
            return ""

        value = value_node.text
        if cell_type == "s":
            index = int(value)
            return shared_strings[index] if index < len(shared_strings) else ""
        return value

    def _distribute_species(self, layer, group_features, species_by_group):
        species_values = {}
        for group, feature_ids in group_features.items():
            pool = list(species_by_group.get(group, []))
            if len(pool) != len(feature_ids):
                raise Exception(
                    f"A planilha possui {len(pool)} muda(s) para {group}, "
                    f"mas a grade criou {len(feature_ids)} ponto(s) desse grupo."
                )

            ordered_ids = self._ordered_feature_ids(layer, feature_ids)
            for feature_id, species in zip(ordered_ids, self._shuffle_without_long_sequences(pool)):
                species_values[feature_id] = species

        return species_values

    def _ordered_feature_ids(self, layer, feature_ids):
        selected = set(feature_ids)
        items = []
        for feature in layer.getFeatures():
            if feature.id() in selected:
                point = feature.geometry().asPoint()
                items.append((point.y(), point.x(), feature.id()))
        return [feature_id for _, _, feature_id in sorted(items)]

    def _shuffle_without_long_sequences(self, species):
        shuffled = list(species)
        for _ in range(200):
            random.shuffle(shuffled)
            if self._has_no_long_sequence(shuffled):
                return shuffled

        ordered = []
        remaining = list(species)
        while remaining:
            candidates = list(range(len(remaining)))
            random.shuffle(candidates)
            picked = None
            for index in candidates:
                if len(ordered) < 2 or not (ordered[-1] == ordered[-2] == remaining[index]):
                    picked = index
                    break
            if picked is None:
                picked = 0
            ordered.append(remaining.pop(picked))
        return ordered

    def _has_no_long_sequence(self, values):
        return all(
            not (values[index] == values[index - 1] == values[index - 2])
            for index in range(2, len(values))
        )

    def _normalize_header(self, value):
        text = unicodedata.normalize("NFKD", str(value).strip().lower())
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", text)

    def _find_column(self, headers, options):
        for option in options:
            if option in headers:
                return headers.index(option)
        raise Exception("A planilha precisa ter colunas de especie, quantidade e classificacao.")

    def _cell(self, row, index):
        return str(row[index]) if index < len(row) else ""

    def _column_index(self, reference):
        letters = re.sub(r"[^A-Z]", "", reference.upper())
        if not letters:
            return -1
        index = 0
        for letter in letters:
            index = index * 26 + ord(letter) - ord("A") + 1
        return index - 1

    def _warn(self, message):
        QMessageBox.warning(self.iface.mainWindow(), "Linhas de Plantio", message)
        self.iface.messageBar().pushWarning("Linhas de Plantio", message)
