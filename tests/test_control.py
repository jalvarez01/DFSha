"""
Tests del ControlNode: registro/heartbeat de DataNodes, el algoritmo de
particionamiento/placement con réplicas y el flujo de dos fases de
escritura (allocate -> los DataNodes informan -> confirm) más el plan de
lectura (blocks) y el borrado distribuido.

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
from tests.helpers import (
    cs,
    deletions_for,
    heartbeat,
    node_headers,
    register,
    register_n,
    report_stored,
    store_block,
    write_file,
)


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


def _nodes_of(block):
    return [n["node_id"] for n in block["datanodes"]]


def _ids_by_node(plan):
    by_node: dict[str, set[str]] = {}
    for b in plan["blocks"]:
        for node in _nodes_of(b):
            by_node.setdefault(node, set()).add(b["block_id"])
    return by_node


# --------------------------------------------------------------------------
# DataNodes: register / heartbeat / list (y su autenticación)
# --------------------------------------------------------------------------
def test_register_datanode_lo_crea_y_queda_vivo(client):
    resp = register(client, "dn-1", host="10.0.0.5", port=9001)
    assert resp.status_code == 200
    body = resp.json()
    assert body["node_id"] == "dn-1"
    assert body["host"] == "10.0.0.5"
    assert body["port"] == 9001
    assert body["status"] == "alive"


def test_register_es_idempotente_por_node_id(client):
    register(client, "dn-1", host="10.0.0.5", port=9001)
    # Reinicio del DataNode: mismo node_id, nuevo host/puerto.
    resp = register(client, "dn-1", host="10.0.0.9", port=9999)
    assert resp.status_code == 200
    assert resp.json()["host"] == "10.0.0.9"
    assert resp.json()["port"] == 9999

    listing = client.get("/datanodes").json()
    assert len([n for n in listing if n["node_id"] == "dn-1"]) == 1


def test_register_sin_token_de_nodo_es_401(client):
    resp = client.post("/datanodes/register", json={"node_id": "dn-1", "host": "h", "port": 1})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_node_token"


def test_un_token_de_usuario_no_sirve_como_token_de_nodo(client):
    from app.security import create_access_token

    user_token, _ = create_access_token(username="tester", user_id=1)
    resp = client.post(
        "/datanodes/register",
        json={"node_id": "dn-1", "host": "h", "port": 1},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 401


def test_un_datanode_no_puede_hacerse_pasar_por_otro(client):
    register(client, "dn-1")
    resp = client.post("/datanodes/heartbeat", json={"node_id": "dn-1"}, headers=node_headers("dn-2"))
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "node_mismatch"


def test_register_guarda_direccion_interna_y_esquema(client):
    resp = register(client, "dn-1", host="localhost", port=8001, internal_host="datanode-1", scheme="https")
    assert resp.status_code == 200
    register_n(client, 0)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 5}).json()
    location = plan["blocks"][0]["datanode"]
    # Al cliente se le da la dirección pública, con su esquema.
    assert (location["host"], location["port"], location["scheme"]) == ("localhost", 8001, "https")


def test_heartbeat_de_nodo_desconocido_es_404(client):
    resp = heartbeat(client, "fantasma")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_datanode"


def test_heartbeat_refresca_el_nodo(client):
    register(client, "dn-1")
    resp = heartbeat(client, "dn-1")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"
    assert resp.json()["role"] == "leader"


# --------------------------------------------------------------------------
# allocate: plan de escritura, round-robin y cadenas de réplica
# --------------------------------------------------------------------------
def test_allocate_sin_datanodes_vivos_es_503(client):
    resp = client.post("/files/grande.bin/allocate", json={"size_bytes": 1024})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "no_datanodes"


def test_allocate_particiona_por_block_size(client):
    register_n(client, 3)
    # 20 bytes con bloques de 8 -> 3 bloques: 8, 8, 4.
    resp = client.post("/files/data.bin/allocate", json={"size_bytes": 20, "block_size": 8})
    assert resp.status_code == 200
    body = resp.json()
    assert body["block_size"] == 8
    assert body["total_size"] == 20
    assert [b["size_bytes"] for b in body["blocks"]] == [8, 8, 4]
    assert [b["index"] for b in body["blocks"]] == [0, 1, 2]
    assert len({b["block_id"] for b in body["blocks"]}) == 3


def test_allocate_reparte_round_robin_entre_nodos_vivos(client):
    register_n(client, 3)
    resp = client.post("/files/data.bin/allocate", json={"size_bytes": 60, "block_size": 10})  # 6 bloques
    primaries = [b["datanode"]["node_id"] for b in resp.json()["blocks"]]
    # El primario de cada cadena rota por el anillo, arrancando en dn-0.
    assert primaries == ["dn-0", "dn-1", "dn-2", "dn-0", "dn-1", "dn-2"]


def test_cada_bloque_va_a_replication_factor_nodos_distintos(client):
    register_n(client, 4)
    plan = client.post("/files/data.bin/allocate", json={"size_bytes": 40, "block_size": 10}).json()
    assert plan["replication"] == settings.replication_factor == 3
    for block in plan["blocks"]:
        nodes = _nodes_of(block)
        assert len(nodes) == 3
        assert len(set(nodes)) == 3  # nunca dos réplicas en el mismo nodo
        assert block["datanode"]["node_id"] == nodes[0]
    # Las cadenas son los siguientes nodos del anillo.
    assert _nodes_of(plan["blocks"][0]) == ["dn-0", "dn-1", "dn-2"]
    assert _nodes_of(plan["blocks"][3]) == ["dn-3", "dn-0", "dn-1"]


def test_con_menos_nodos_que_el_factor_se_usan_todos(client):
    register_n(client, 2)
    plan = client.post("/files/data.bin/allocate", json={"size_bytes": 10}).json()
    assert plan["replication"] == 2
    assert sorted(_nodes_of(plan["blocks"][0])) == ["dn-0", "dn-1"]


def test_allocate_devuelve_token_de_escritura_con_la_cadena(client):
    from app.cluster_auth import decode_cluster_token

    register_n(client, 3)
    block = client.post("/files/data.bin/allocate", json={"size_bytes": 5}).json()["blocks"][0]
    payload = decode_cluster_token(block["token"], "block")
    assert payload["op"] == "write"
    assert payload["bid"] == block["block_id"]
    assert [p["node_id"] for p in payload["pipe"]] == _nodes_of(block)


def test_allocate_usa_block_size_por_defecto_de_settings(client):
    register_n(client, 2)
    resp = client.post("/files/chico.bin/allocate", json={"size_bytes": 100})
    assert resp.json()["block_size"] == settings.block_size_bytes
    assert len(resp.json()["blocks"]) == 1


def test_allocate_archivo_vacio_no_crea_bloques(client):
    register_n(client, 2)
    resp = client.post("/files/vacio.bin/allocate", json={"size_bytes": 0})
    assert resp.status_code == 200
    assert resp.json()["blocks"] == []


def test_allocate_falla_si_el_padre_no_existe(client):
    register_n(client, 1)
    resp = client.post("/files/no/existe/data.bin/allocate", json={"size_bytes": 10})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_allocate_sobre_directorio_existente_es_409(client):
    register_n(client, 1)
    client.post("/fs/mkdir", json={"path": "/documentos"})
    resp = client.post("/files/documentos/allocate", json={"size_bytes": 10})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "not_a_directory"


def test_reallocate_reemplaza_el_plan_anterior(client):
    register_n(client, 2)
    first = write_file(client, "/data.bin", 30)
    second = client.post("/files/data.bin/allocate", json={"size_bytes": 10, "block_size": 10}).json()
    assert first["file_id"] == second["file_id"]
    assert len(second["blocks"]) == 1
    assert {b["block_id"] for b in first["blocks"]}.isdisjoint({b["block_id"] for b in second["blocks"]})


# --------------------------------------------------------------------------
# Flujo completo: allocate -> los DataNodes informan -> confirm -> blocks
# --------------------------------------------------------------------------
def test_flujo_dos_fases_completo(client):
    register_n(client, 3)
    plan = client.post("/files/informe.pdf/allocate", json={"size_bytes": 25, "block_size": 10}).json()

    # El plan de lectura antes de confirmar no expone bloques.
    assert client.get("/files/informe.pdf/blocks").json()["num_blocks"] == 0

    confirmations = [store_block(client, b, checksum=cs(b["index"])) for b in plan["blocks"]]
    confirmed = client.post(
        "/files/informe.pdf/confirm", json={"blocks": confirmations, "lease_id": plan["lease_id"]}
    ).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 3
    assert confirmed["size_bytes"] == 25

    read_plan = client.get("/files/informe.pdf/blocks").json()
    assert read_plan["num_blocks"] == 3
    assert read_plan["size_bytes"] == 25
    assert [b["index"] for b in read_plan["blocks"]] == [0, 1, 2]
    assert [b["checksum"] for b in read_plan["blocks"]] == [cs(0), cs(1), cs(2)]
    for b in read_plan["blocks"]:
        # Todas las réplicas, con dirección alcanzable y token de lectura.
        assert len(b["datanodes"]) == 3
        assert all(n["host"].startswith("10.0.0.") for n in b["datanodes"])
        assert b["token"]


def test_confirm_sin_informes_de_los_datanodes_es_409(client):
    """El ControlNode no se fía del cliente: si ningún DataNode informó
    haber guardado el bloque, no se confirma."""
    register_n(client, 2)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 10}).json()
    block = plan["blocks"][0]
    resp = client.post(
        "/files/a.bin/confirm",
        json={"blocks": [{"block_id": block["block_id"], "checksum": cs("x"), "size_bytes": 10}]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "replicas_not_ready"


def test_confirm_con_checksum_distinto_al_de_los_datanodes_es_409(client):
    register_n(client, 2)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 10}).json()
    store_block(client, plan["blocks"][0], checksum=cs("lo-que-guardaron"))
    resp = client.post(
        "/files/a.bin/confirm",
        json={"blocks": [{"block_id": plan["blocks"][0]["block_id"], "checksum": cs("otra-cosa"), "size_bytes": 10}]},
    )
    assert resp.status_code == 409


def test_confirm_con_una_sola_replica_basta_y_el_resto_se_descarta(client):
    """min_replicas=1: si solo un nodo de la cadena guardó el bloque, se
    confirma igual; los otros salen del plan para que el re-replicador los
    complete."""
    register_n(client, 3)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 10}).json()
    block = plan["blocks"][0]
    confirmation = store_block(client, block, nodes=["dn-0"])
    resp = client.post("/files/a.bin/confirm", json={"blocks": [confirmation]})
    assert resp.status_code == 200
    read = client.get("/files/a.bin/blocks").json()["blocks"][0]
    assert _nodes_of(read) == ["dn-0"]


def test_confirm_parcial_marca_incompleto(client):
    register_n(client, 2)
    plan = client.post("/files/data.bin/allocate", json={"size_bytes": 20, "block_size": 10}).json()
    confirmation = store_block(client, plan["blocks"][0])
    confirmed = client.post("/files/data.bin/confirm", json={"blocks": [confirmation]}).json()
    assert confirmed["complete"] is False
    assert confirmed["num_blocks"] == 1
    assert confirmed["size_bytes"] == 10


def test_confirm_de_bloque_ajeno_es_400(client):
    register_n(client, 1)
    client.post("/files/data.bin/allocate", json={"size_bytes": 10, "block_size": 10})
    resp = client.post(
        "/files/data.bin/confirm",
        json={"blocks": [{"block_id": "no-existe", "checksum": cs(1), "size_bytes": 10}]},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_block"


def test_blocks_de_archivo_inexistente_es_404(client):
    register_n(client, 1)
    resp = client.get("/files/fantasma.bin/blocks")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


def test_plan_de_lectura_pone_primero_las_replicas_vivas(client, db):
    from datetime import datetime, timedelta, timezone

    from app.models import DataNode

    register_n(client, 3)
    write_file(client, "/a.bin", 10)
    # dn-0 deja de latir.
    node = db.query(DataNode).filter_by(node_id="dn-0").one()
    node.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=settings.heartbeat_ttl_seconds + 5)
    db.commit()
    read = client.get("/files/a.bin/blocks").json()["blocks"][0]
    assert _nodes_of(read)[-1] == "dn-0"
    assert set(_nodes_of(read)[:2]) == {"dn-1", "dn-2"}


# --------------------------------------------------------------------------
# CRUD de un bloque puntual: POST /files/{path}/blocks/{index}/allocate
# --------------------------------------------------------------------------
def test_allocate_block_no_choca_con_el_allocate_de_archivo_completo(client):
    """Regresión del problema de ruteo: '/{path:path}/allocate' es greedy."""
    register_n(client, 2)
    write_file(client, "/data.bin", 0)
    resp = client.post("/files/data.bin/blocks/0/allocate", json={"size_bytes": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["index"] == 0
    assert body["size_bytes"] == 5
    assert len(body["datanodes"]) == 2


def test_allocate_block_anexa_al_final(client):
    register_n(client, 2)
    plan = write_file(client, "/data.bin", 20)  # índices 0 y 1
    appended = client.post("/files/data.bin/blocks/2/allocate", json={"size_bytes": 4}).json()
    assert appended["index"] == 2
    assert appended["file_id"] == plan["file_id"]

    confirmation = store_block(client, appended)
    confirmed = client.post(
        "/files/data.bin/confirm", json={"blocks": [confirmation], "lease_id": appended["lease_id"]}
    ).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 3
    assert confirmed["size_bytes"] == 24


def test_allocate_block_reemplaza_un_bloque_existente_sin_tocar_los_demas(client):
    register_n(client, 2)
    plan = write_file(client, "/data.bin", 20, checksums=[cs(0), cs(1)])
    old_block_1_id = plan["blocks"][1]["block_id"]
    replaced = client.post("/files/data.bin/blocks/1/allocate", json={"size_bytes": 7}).json()
    assert replaced["index"] == 1
    assert replaced["block_id"] != old_block_1_id

    # Mientras el reemplazo está pendiente, se sigue sirviendo la versión vieja.
    read_plan = client.get("/files/data.bin/blocks").json()
    assert read_plan["num_blocks"] == 2
    assert [b["checksum"] for b in read_plan["blocks"]] == [cs(0), cs(1)]
    assert read_plan["blocks"][1]["block_id"] == old_block_1_id

    confirmation = store_block(client, replaced, checksum=cs("v2"))
    client.post("/files/data.bin/confirm", json={"blocks": [confirmation], "lease_id": replaced["lease_id"]})
    read_plan = client.get("/files/data.bin/blocks").json()
    assert read_plan["num_blocks"] == 2
    assert read_plan["size_bytes"] == 10 + 7
    assert read_plan["blocks"][1]["checksum"] == cs("v2")
    assert read_plan["blocks"][1]["block_id"] == replaced["block_id"]


def test_allocate_block_con_gap_es_400(client):
    register_n(client, 1)
    write_file(client, "/data.bin", 10)
    resp = client.post("/files/data.bin/blocks/5/allocate", json={"size_bytes": 3})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_block_index"


def test_allocate_block_sin_datanodes_vivos_es_503(client):
    client.post("/files/data.bin/allocate", json={"size_bytes": 0})
    client.post("/files/data.bin/confirm", json={"blocks": []})
    resp = client.post("/files/data.bin/blocks/0/allocate", json={"size_bytes": 3})
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "no_datanodes"


def test_allocate_block_de_archivo_inexistente_es_404(client):
    register_n(client, 1)
    resp = client.post("/files/fantasma.bin/blocks/0/allocate", json={"size_bytes": 3})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "not_found"


# --------------------------------------------------------------------------
# RF1 distribuido: órdenes de borrado a CADA réplica, vía heartbeat
# --------------------------------------------------------------------------
def test_heartbeat_sin_pendientes_no_ordena_borrar_nada(client):
    register_n(client, 1)
    assert deletions_for(client, "dn-0") == set()


def test_rm_ordena_borrar_cada_replica_a_su_datanode(client):
    register_n(client, 3)
    plan = write_file(client, "/a.bin", 30)
    expected = _ids_by_node(plan)
    assert client.request("DELETE", "/fs/rm", json={"path": "/a.bin"}).status_code == 200
    for node_id in ("dn-0", "dn-1", "dn-2"):
        assert deletions_for(client, node_id) == expected[node_id]


def test_la_orden_se_repite_hasta_que_el_datanode_la_acusa(client):
    register_n(client, 1)
    plan = write_file(client, "/a.bin", 20)
    ids = {b["block_id"] for b in plan["blocks"]}
    client.request("DELETE", "/fs/rm", json={"path": "/a.bin"})
    assert deletions_for(client, "dn-0") == ids
    assert deletions_for(client, "dn-0") == ids
    assert deletions_for(client, "dn-0", deleted=ids) == set()


def test_la_orden_espera_el_periodo_de_gracia(client, monkeypatch):
    """Un lector que pidió el plan justo antes del rm puede seguir bajando
    los bloques durante deletion_grace_seconds."""
    monkeypatch.setattr(settings, "deletion_grace_seconds", 3600.0)
    register_n(client, 1)
    write_file(client, "/a.bin", 10)
    client.request("DELETE", "/fs/rm", json={"path": "/a.bin"})
    assert deletions_for(client, "dn-0") == set()


def test_rmdir_recursivo_ordena_borrar_los_bloques_de_todo_el_subarbol(client):
    register_n(client, 1)
    client.post("/fs/mkdir", json={"path": "/d/e", "parents": True})
    p1 = write_file(client, "/d/uno.bin", 10)
    p2 = write_file(client, "/d/e/dos.bin", 25)
    resp = client.request("DELETE", "/fs/rmdir", json={"path": "/d", "recursive": True})
    assert resp.status_code == 200
    assert deletions_for(client, "dn-0") == {b["block_id"] for b in p1["blocks"] + p2["blocks"]}


def test_rm_de_archivo_a_medio_subir_tambien_ordena_borrar(client):
    """Un bloque sin confirmar pudo llegar a subirse al DataNode."""
    register_n(client, 1)
    plan = client.post("/files/a.bin/allocate", json={"size_bytes": 10, "block_size": 10}).json()
    client.request("DELETE", "/fs/rm", json={"path": "/a.bin"})
    assert deletions_for(client, "dn-0") == {plan["blocks"][0]["block_id"]}


def test_reallocate_ordena_borrar_el_plan_anterior(client):
    register_n(client, 1)
    first = write_file(client, "/a.bin", 20)
    client.post("/files/a.bin/allocate", json={"size_bytes": 5, "block_size": 10})
    assert deletions_for(client, "dn-0") == {b["block_id"] for b in first["blocks"]}


def test_confirmar_un_reemplazo_ordena_borrar_solo_el_bloque_viejo(client):
    register_n(client, 1)
    plan = write_file(client, "/a.bin", 20)
    old_id = plan["blocks"][1]["block_id"]
    new = client.post("/files/a.bin/blocks/1/allocate", json={"size_bytes": 3}).json()
    assert deletions_for(client, "dn-0") == set()  # el viejo sigue en uso

    confirmation = store_block(client, new)
    client.post("/files/a.bin/confirm", json={"blocks": [confirmation], "lease_id": new["lease_id"]})
    assert deletions_for(client, "dn-0") == {old_id}


def test_dos_reemplazos_seguidos_descartan_el_pendiente_anterior(client):
    register_n(client, 1)
    write_file(client, "/a.bin", 20)
    first = client.post("/files/a.bin/blocks/0/allocate", json={"size_bytes": 3}).json()
    second = client.post(
        "/files/a.bin/blocks/0/allocate", json={"size_bytes": 4, "lease_id": first["lease_id"]}
    ).json()
    assert deletions_for(client, "dn-0") == {first["block_id"]}

    confirmation = store_block(client, second)
    confirmed = client.post(
        "/files/a.bin/confirm", json={"blocks": [confirmation], "lease_id": second["lease_id"]}
    ).json()
    assert confirmed["complete"] is True
    assert confirmed["num_blocks"] == 2
    assert confirmed["size_bytes"] == 4 + 10


def test_bloque_informado_que_el_namespace_no_conoce_se_manda_borrar(client):
    register_n(client, 1)
    report_stored(client, "dn-0", "bloque-huerfano", cs("x"), 3)
    assert deletions_for(client, "dn-0") == {"bloque-huerfano"}
