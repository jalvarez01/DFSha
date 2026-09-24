"""
Tests del ControlNode (Hito 2): registro/heartbeat de DataNodes, el
algoritmo de particionamiento/placement y el flujo de dos fases de
escritura (allocate -> confirm) más el plan de lectura (blocks).

Se monta un mini-app con los routers de datanodes y control (más el de
fs para poder crear directorios), sobre la SQLite en memoria de la fixture
`db`, sin arrastrar el lifespan de app.main.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import placement
from app.config import settings
from app.database import get_db
from app.routers import control as control_module
from app.routers import datanodes as datanodes_module
from app.routers import fs as fs_module


@pytest.fixture()
def client(db):
    # Cursor del round-robin determinista en cada test.
    placement.reset_cursor()

    app = FastAPI()
    app.include_router(datanodes_module.router, prefix="/datanodes")
    app.include_router(control_module.router, prefix="/files")
    app.include_router(fs_module.router, prefix="/fs")
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as test_client:
        test_client.headers.update({"X-Username": "tester"})
        yield test_client


def _register(client, node_id, host="10.0.0.1", port=9000):
    return client.post(
        "/datanodes/register",
        json={"node_id": node_id, "host": host, "port": port},
    )


def _register_n(client, n):
    """Registra n DataNodes vivos: dn-0 .. dn-(n-1)."""
    for i in range(n):
        _register(client, f"dn-{i}", host=f"10.0.0.{i + 1}", port=9000 + i)


# --------------------------------------------------------------------------
# DataNodes: register / heartbeat / list
# --------------------------------------------------------------------------
def test_register_datanode_lo_crea_y_queda_vivo(client):
    resp = _register(client, "dn-1", host="10.0.0.5", port=9001)
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_id"] == "dn-1"
    assert body["host"] == "10.0.0.5"
    assert body["port"] == 9001
    assert body["status"] == "alive"


def test_register_es_idempotente_por_node_id(client):
    _register(client, "dn-1", host="10.0.0.5", port=9001)
    # Reinicio del DataNode: mismo node_id, nuevo host/puerto.
    resp = _register(client, "dn-1", host="10.0.0.9", port=9999)
    assert resp.status_code == 200
    assert resp.json()["host"] == "10.0.0.9"
    assert resp.json()["port"] == 9999

    listing = client.get("/datanodes").json()
    assert len([n for n in listing if n["node_id"] == "dn-1"]) == 1


def test_heartbeat_de_nodo_desconocido_es_404(client):
    resp = client.post("/datanodes/heartbeat", json={"node_id": "fantasma"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_datanode"


def test_heartbeat_refresca_el_nodo(client):
    _register(client, "dn-1")
    resp = client.post("/datanodes/heartbeat", json={"node_id": "dn-1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


# --------------------------------------------------------------------------
# allocate: plan de escritura y round-robin
# --------------------------------------------------------------------------
def test_allocate_sin_datanodes_vivos_es_503(client):
    resp = client.post("/files/grande.bin/allocate", json={"size_bytes": 1024})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "no_datanodes"


def test_allocate_particiona_por_block_size(client):
    _register_n(client, 3)
    # 20 bytes con bloques de 8 -> 3 bloques: 8, 8, 4.
    resp = client.post(
        "/files/data.bin/allocate",
        json={"size_bytes": 20, "block_size": 8},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["block_size"] == 8
    assert body["total_size"] == 20
    sizes = [b["size_bytes"] for b in body["blocks"]]
    assert sizes == [8, 8, 4]
    assert [b["index"] for b in body["blocks"]] == [0, 1, 2]
    # block_id únicos
    assert len({b["block_id"] for b in body["blocks"]}) == 3


def test_allocate_reparte_round_robin_entre_nodos_vivos(client):
    _register_n(client, 3)
    resp = client.post(
        "/files/data.bin/allocate",
        json={"size_bytes": 60, "block_size": 10},  # 6 bloques
    )
    nodes = [b["datanode"]["node_id"] for b in resp.json()["blocks"]]
    # 6 bloques sobre 3 nodos, round-robin arrancando en dn-0 (cursor reset).
    assert nodes == ["dn-0", "dn-1", "dn-2", "dn-0", "dn-1", "dn-2"]


def test_allocate_usa_block_size_por_defecto_de_settings(client):
    _register_n(client, 2)
    resp = client.post("/files/chico.bin/allocate", json={"size_bytes": 100})
    assert resp.json()["block_size"] == settings.block_size_bytes
    # 100 bytes < 8 MiB -> un solo bloque.
    assert len(resp.json()["blocks"]) == 1


def test_allocate_archivo_vacio_no_crea_bloques(client):
    _register_n(client, 2)
    resp = client.post("/files/vacio.bin/allocate", json={"size_bytes": 0})
    assert resp.status_code == 200
    assert resp.json()["blocks"] == []


def test_allocate_falla_si_el_padre_no_existe(client):
    _register_n(client, 1)
    resp = client.post("/files/no/existe/data.bin/allocate", json={"size_bytes": 10})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_allocate_sobre_directorio_existente_es_409(client):
    _register_n(client, 1)
    client.post("/fs/mkdir", json={"path": "/documentos"})
    resp = client.post("/files/documentos/allocate", json={"size_bytes": 10})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_reallocate_reemplaza_el_plan_anterior(client):
    _register_n(client, 2)
    first = client.post("/files/data.bin/allocate", json={"size_bytes": 30, "block_size": 10}).json()
    second = client.post("/files/data.bin/allocate", json={"size_bytes": 10, "block_size": 10}).json()
    # Mismo archivo (mismo file_id), plan nuevo con distintos block_id.
    assert first["file_id"] == second["file_id"]
    assert len(second["blocks"]) == 1
    old_ids = {b["block_id"] for b in first["blocks"]}
    new_ids = {b["block_id"] for b in second["blocks"]}
    assert old_ids.isdisjoint(new_ids)


# --------------------------------------------------------------------------
# Flujo completo: allocate -> (subida simulada) -> confirm -> blocks
# --------------------------------------------------------------------------
def test_flujo_dos_fases_completo(client):
    _register_n(client, 3)
    plan = client.post(
        "/files/informe.pdf/allocate",
        json={"size_bytes": 25, "block_size": 10},  # 3 bloques: 10,10,5
    ).json()

    # El plan de lectura antes de confirmar no expone bloques.
    pre = client.get("/files/informe.pdf/blocks").json()
    assert pre["num_blocks"] == 0

    # El cliente sube cada bloque y reporta checksum/tamaño reales.
    confirm_body = {
        "blocks": [
            {"block_id": b["block_id"], "checksum": f"sha-{b['index']}", "size_bytes": b["size_bytes"]}
            for b in plan["blocks"]
        ]
    }
    confirmed = client.post("/files/informe.pdf/confirm", json=confirm_body).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 3
    assert confirmed["size_bytes"] == 25

    # Ahora el plan de lectura devuelve los bloques en orden y con checksum.
    read_plan = client.get("/files/informe.pdf/blocks").json()
    assert read_plan["num_blocks"] == 3
    assert read_plan["size_bytes"] == 25
    assert [b["index"] for b in read_plan["blocks"]] == [0, 1, 2]
    assert [b["checksum"] for b in read_plan["blocks"]] == ["sha-0", "sha-1", "sha-2"]
    # Cada bloque trae su DataNode alcanzable.
    for b in read_plan["blocks"]:
        assert b["datanode"]["host"].startswith("10.0.0.")
        assert b["datanode"]["port"] >= 9000


def test_confirm_parcial_marca_incompleto(client):
    _register_n(client, 2)
    plan = client.post(
        "/files/data.bin/allocate",
        json={"size_bytes": 20, "block_size": 10},  # 2 bloques
    ).json()
    first_block = plan["blocks"][0]
    confirmed = client.post(
        "/files/data.bin/confirm",
        json={"blocks": [{"block_id": first_block["block_id"], "checksum": "abc", "size_bytes": 10}]},
    ).json()
    assert confirmed["complete"] is False
    assert confirmed["num_blocks"] == 1
    assert confirmed["size_bytes"] == 10


def test_confirm_de_bloque_ajeno_es_400(client):
    _register_n(client, 1)
    client.post("/files/data.bin/allocate", json={"size_bytes": 10, "block_size": 10})
    resp = client.post(
        "/files/data.bin/confirm",
        json={"blocks": [{"block_id": "no-existe", "checksum": "abc", "size_bytes": 10}]},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_block"


def test_blocks_de_archivo_inexistente_es_404(client):
    _register_n(client, 1)
    resp = client.get("/files/fantasma.bin/blocks")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# CRUD de un bloque puntual: POST /files/{path}/blocks/{index}/allocate
# (decisión de equipo: acceso CRUD, no WORM — permite actualizar/anexar un
# solo bloque sin re-subir todo el archivo).
# --------------------------------------------------------------------------
def test_allocate_block_no_choca_con_el_allocate_de_archivo_completo(client):
    """Regresión del problema de ruteo: '/{path:path}/allocate' es greedy y
    calzaría también con '.../blocks/0/allocate' si esa ruta más específica
    no se registrara primero."""
    _register_n(client, 2)
    client.post("/files/data.bin/allocate", json={"size_bytes": 0})  # crea el File, sin bloques
    resp = client.post("/files/data.bin/blocks/0/allocate", json={"size_bytes": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["index"] == 0
    assert body["size_bytes"] == 5
    assert "datanode" in body


def test_allocate_block_anexa_al_final(client):
    _register_n(client, 2)
    plan = client.post(
        "/files/data.bin/allocate", json={"size_bytes": 20, "block_size": 10}
    ).json()  # 2 bloques: index 0, 1
    resp = client.post("/files/data.bin/blocks/2/allocate", json={"size_bytes": 4})
    assert resp.status_code == 200
    appended = resp.json()
    assert appended["index"] == 2
    assert appended["file_id"] == plan["file_id"]

    # confirmar el nuevo bloque + los dos anteriores, sin re-subirlos
    confirm_body = {
        "blocks": [
            {"block_id": b["block_id"], "checksum": f"sha-{b['index']}", "size_bytes": b["size_bytes"]}
            for b in plan["blocks"]
        ]
        + [{"block_id": appended["block_id"], "checksum": "sha-2", "size_bytes": 4}]
    }
    confirmed = client.post("/files/data.bin/confirm", json=confirm_body).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 3
    assert confirmed["size_bytes"] == 24


def test_allocate_block_reemplaza_un_bloque_existente_sin_tocar_los_demas(client):
    _register_n(client, 2)
    plan = client.post(
        "/files/data.bin/allocate", json={"size_bytes": 20, "block_size": 10}
    ).json()
    confirm_body = {
        "blocks": [
            {"block_id": b["block_id"], "checksum": f"sha-{b['index']}", "size_bytes": b["size_bytes"]}
            for b in plan["blocks"]
        ]
    }
    client.post("/files/data.bin/confirm", json=confirm_body)

    old_block_1_id = plan["blocks"][1]["block_id"]
    replaced = client.post("/files/data.bin/blocks/1/allocate", json={"size_bytes": 7}).json()
    assert replaced["index"] == 1
    assert replaced["block_id"] != old_block_1_id

    # Mientras el reemplazo está pendiente, el plan de lectura sigue
    # sirviendo la versión vieja del bloque 1: un lector nunca ve un hueco.
    read_plan = client.get("/files/data.bin/blocks").json()
    assert read_plan["num_blocks"] == 2
    assert [b["checksum"] for b in read_plan["blocks"]] == ["sha-0", "sha-1"]
    assert read_plan["blocks"][1]["block_id"] == old_block_1_id

    # confirmar el reemplazo
    client.post(
        "/files/data.bin/confirm",
        json={"blocks": [{"block_id": replaced["block_id"], "checksum": "sha-1-v2", "size_bytes": 7}]},
    )
    read_plan = client.get("/files/data.bin/blocks").json()
    assert read_plan["num_blocks"] == 2
    assert read_plan["size_bytes"] == 10 + 7  # bloque 0 sin tocar + bloque 1 nuevo
    assert read_plan["blocks"][1]["checksum"] == "sha-1-v2"
    assert read_plan["blocks"][1]["block_id"] == replaced["block_id"]


def test_allocate_block_con_gap_es_400(client):
    _register_n(client, 1)
    client.post("/files/data.bin/allocate", json={"size_bytes": 10, "block_size": 10})  # 1 bloque
    resp = client.post("/files/data.bin/blocks/5/allocate", json={"size_bytes": 3})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_block_index"


def test_allocate_block_sin_datanodes_vivos_es_503(client):
    # Un allocate de archivo vacío no exige nodos vivos, así que el File
    # puede existir sin que haya habido ningún DataNode registrado nunca.
    client.post("/files/data.bin/allocate", json={"size_bytes": 0})
    resp = client.post("/files/data.bin/blocks/0/allocate", json={"size_bytes": 3})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "no_datanodes"


def test_allocate_block_de_archivo_inexistente_es_404(client):
    _register_n(client, 1)
    resp = client.post("/files/fantasma.bin/blocks/0/allocate", json={"size_bytes": 3})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# RF1 distribuido: órdenes de borrado entregadas en el heartbeat
# --------------------------------------------------------------------------
def _heartbeat(client, node_id, deleted=()):
    resp = client.post(
        "/datanodes/heartbeat", json={"node_id": node_id, "deleted_blocks": list(deleted)}
    )
    assert resp.status_code == 200
    return resp.json()["delete_blocks"]


def _write(client, path, size, block_size=10):
    """allocate + confirm de un archivo; devuelve el plan de escritura."""
    plan = client.post(f"/files{path}/allocate", json={"size_bytes": size, "block_size": block_size}).json()
    client.post(
        f"/files{path}/confirm",
        json={
            "blocks": [
                {"block_id": b["block_id"], "checksum": f"sha-{b['index']}", "size_bytes": b["size_bytes"]}
                for b in plan["blocks"]
            ]
        },
    )
    return plan


def _ids_by_node(plan):
    by_node: dict[str, set[str]] = {}
    for b in plan["blocks"]:
        by_node.setdefault(b["datanode"]["node_id"], set()).add(b["block_id"])
    return by_node


def test_heartbeat_sin_pendientes_no_ordena_borrar_nada(client):
    _register_n(client, 1)
    assert _heartbeat(client, "dn-0") == []


def test_rm_ordena_borrar_los_bloques_a_cada_datanode(client):
    _register_n(client, 2)
    plan = _write(client, "/a.bin", 30)  # 3 bloques en dn-0, dn-1, dn-0
    expected = _ids_by_node(plan)

    assert client.request("DELETE", "/fs/rm", json={"path": "/a.bin"}).status_code == 200

    assert set(_heartbeat(client, "dn-0")) == expected["dn-0"]
    assert set(_heartbeat(client, "dn-1")) == expected["dn-1"]


def test_la_orden_se_repite_hasta_que_el_datanode_la_acusa(client):
    _register_n(client, 1)
    plan = _write(client, "/a.bin", 20)
    ids = {b["block_id"] for b in plan["blocks"]}
    client.request("DELETE", "/fs/rm", json={"path": "/a.bin"})

    # Sin acuse (p. ej. el DataNode se cayó a mitad), la orden vuelve a salir.
    assert set(_heartbeat(client, "dn-0")) == ids
    assert set(_heartbeat(client, "dn-0")) == ids
    # Con acuse, desaparece.
    assert _heartbeat(client, "dn-0", deleted=ids) == []


def test_rmdir_recursivo_ordena_borrar_los_bloques_de_todo_el_subarbol(client):
    _register_n(client, 1)
    client.post("/fs/mkdir", json={"path": "/d/e", "parents": True})
    p1 = _write(client, "/d/uno.bin", 10)
    p2 = _write(client, "/d/e/dos.bin", 25)

    resp = client.request("DELETE", "/fs/rmdir", json={"path": "/d", "recursive": True})
    assert resp.status_code == 200

    expected = {b["block_id"] for b in p1["blocks"] + p2["blocks"]}
    assert set(_heartbeat(client, "dn-0")) == expected


def test_rm_de_archivo_a_medio_subir_tambien_ordena_borrar(client):
    """Un bloque sin confirmar pudo llegar a subirse al DataNode."""
    _register_n(client, 1)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 10, "block_size": 10}).json()
    client.request("DELETE", "/fs/rm", json={"path": "/a.bin"})
    assert _heartbeat(client, "dn-0") == [plan["blocks"][0]["block_id"]]


def test_reallocate_ordena_borrar_el_plan_anterior(client):
    _register_n(client, 1)
    first = _write(client, "/a.bin", 20)
    client.post("/files/a.bin/allocate", json={"size_bytes": 5, "block_size": 10})
    assert set(_heartbeat(client, "dn-0")) == {b["block_id"] for b in first["blocks"]}


def test_confirmar_un_reemplazo_ordena_borrar_solo_el_bloque_viejo(client):
    _register_n(client, 1)
    plan = _write(client, "/a.bin", 20)
    old_id = plan["blocks"][1]["block_id"]
    new = client.post("/files/a.bin/blocks/1/allocate", json={"size_bytes": 3}).json()

    # Antes de confirmar, el viejo sigue en uso: nada que borrar.
    assert _heartbeat(client, "dn-0") == []

    client.post(
        "/files/a.bin/confirm",
        json={"blocks": [{"block_id": new["block_id"], "checksum": "v2", "size_bytes": 3}]},
    )
    assert _heartbeat(client, "dn-0") == [old_id]


def test_dos_reemplazos_seguidos_descartan_el_pendiente_anterior(client):
    _register_n(client, 1)
    _write(client, "/a.bin", 20)
    first = client.post("/files/a.bin/blocks/0/allocate", json={"size_bytes": 3}).json()
    second = client.post("/files/a.bin/blocks/0/allocate", json={"size_bytes": 4}).json()

    assert _heartbeat(client, "dn-0") == [first["block_id"]]
    # Confirmar el segundo deja el archivo completo con 2 bloques.
    confirmed = client.post(
        "/files/a.bin/confirm",
        json={"blocks": [{"block_id": second["block_id"], "checksum": "v2", "size_bytes": 4}]},
    ).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 2
    assert confirmed["size_bytes"] == 4 + 10


def test_reemplazo_pendiente_marca_el_archivo_incompleto(client):
    _register_n(client, 1)
    plan = _write(client, "/a.bin", 20)
    client.post("/files/a.bin/blocks/0/allocate", json={"size_bytes": 3})
    # Re-confirmar un bloque ya confirmado no cambia nada, y el reemplazo
    # pendiente sigue haciendo que el archivo no esté completo.
    b1 = plan["blocks"][1]
    confirmed = client.post(
        "/files/a.bin/confirm",
        json={"blocks": [{"block_id": b1["block_id"], "checksum": "sha-1", "size_bytes": 10}]},
    ).json()
    assert confirmed["complete"] is False
    assert confirmed["num_blocks"] == 2
