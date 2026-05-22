from .area_ha_plugin import AreaHaPlugin
from .apps_plugin import CriarAppsPlugin
from .exportar_shp_contexto import ExportarShpContexto
from .fbds_downloader_plugin import FbdsDownloaderPlugin
from .linhas_plantio_plugin import LinhasPlantioPlugin
from .pegar_coordenadas_plugin import PegarCoordenadasPlugin
from .vertices_unicos_plugin import VerticesUnicosPlugin


class UtilidadesQgisUnificadasPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugins = [
            PegarCoordenadasPlugin(iface),
            AreaHaPlugin(iface),
            ExportarShpContexto(iface),
            LinhasPlantioPlugin(iface),
            VerticesUnicosPlugin(iface),
            FbdsDownloaderPlugin(iface),
            CriarAppsPlugin(iface),
        ]

    def initGui(self):
        for plugin in self.plugins:
            plugin.initGui()

    def unload(self):
        for plugin in reversed(self.plugins):
            plugin.unload()
