"""
Tests de pathutils: la lógica de 'cd' y de convertir rutas relativas a
absolutas en el cliente, espejo simplificado de las reglas que usa
app/path_service.py del servidor (ver docstring de pathutils.py).
"""
import pytest

from dfsha_cli.pathutils import leaf_name, normalize_absolute, resolve_path


class TestNormalizeAbsolute:
    def test_ruta_simple(self):
        assert normalize_absolute("/a/b/c") == "/a/b/c"

    def test_raiz(self):
        assert normalize_absolute("/") == "/"

    def test_colapsa_slashes_repetidos(self):
        assert normalize_absolute("/a//b///c") == "/a/b/c"

    def test_ignora_punto(self):
        assert normalize_absolute("/a/./b/./c") == "/a/b/c"

    def test_resuelve_doble_punto(self):
        assert normalize_absolute("/a/b/c/..") == "/a/b"

    def test_doble_punto_en_raiz_no_hace_nada(self):
        assert normalize_absolute("/../../a") == "/a"
        assert normalize_absolute("/..") == "/"

    def test_requiere_ruta_absoluta(self):
        with pytest.raises(ValueError):
            normalize_absolute("relativa/sin/slash")


class TestResolvePath:
    def test_ruta_absoluta_ignora_cwd(self):
        assert resolve_path(cwd="/algun/lugar", path="/x/y") == "/x/y"

    def test_ruta_relativa_se_combina_con_cwd(self):
        assert resolve_path(cwd="/docs", path="reportes") == "/docs/reportes"

    def test_ruta_relativa_desde_raiz(self):
        assert resolve_path(cwd="/", path="a") == "/a"

    def test_punto_significa_quedarse_en_cwd(self):
        assert resolve_path(cwd="/docs/2026", path=".") == "/docs/2026"

    def test_vacio_significa_quedarse_en_cwd(self):
        assert resolve_path(cwd="/docs", path="") == "/docs"

    def test_doble_punto_relativo_sube_un_nivel(self):
        assert resolve_path(cwd="/a/b/c", path="..") == "/a/b"

    def test_combinacion_de_relativos(self):
        assert resolve_path(cwd="/a/b", path="../x/./y") == "/a/x/y"


class TestLeafName:
    def test_archivo_simple(self):
        assert leaf_name("/a/b/archivo.txt") == "archivo.txt"

    def test_raiz(self):
        assert leaf_name("/") == "/"

    def test_un_solo_nivel(self):
        assert leaf_name("/archivo.txt") == "archivo.txt"
