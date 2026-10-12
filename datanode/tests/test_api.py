import hashlib

from tests.conftest import (
    NODE_ID,
    auth,
    controlnode_token,
    get,
    pipe_entry,
    put,
    read_token,
    write_token,
)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["node_id"] == NODE_ID


def test_put_then_get_roundtrip(client):
    put_resp = put(client, "blk-1", b"hola bloque")
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["size_bytes"] == len(b"hola bloque")
    assert body["stored_on"] == [NODE_ID]

    get_resp = get(client, "blk-1")
    assert get_resp.status_code == 200
    assert get_resp.content == b"hola bloque"


def test_get_missing_block_is_404(client):
    assert get(client, "no-existe").status_code == 404


def test_delete_block_con_token_de_controlnode(client):
    put(client, "blk-1", b"data")
    resp = client.delete("/blocks/blk-1", headers=auth(controlnode_token()))
    assert resp.status_code == 200
    assert get(client, "blk-1").status_code == 404


def test_delete_missing_block_is_404(client):
    assert client.delete("/blocks/no-existe", headers=auth(controlnode_token())).status_code == 404


def test_delete_file_blocks(client):
    put(client, "blk-1", b"a")
    put(client, "blk-2", b"b")
    resp = client.delete("/files/7/blocks", headers=auth(controlnode_token()))
    assert resp.status_code == 200
    assert resp.json()["blocks_deleted"] == 2
    assert get(client, "blk-1").status_code == 404


# --------------------------------------------------------------------------
# Autorización: tokens de bloque firmados por el ControlNode
# --------------------------------------------------------------------------
def test_health_no_exige_token(client):
    assert client.get("/health").status_code == 200


def test_sin_token_es_401(client):
    assert client.put("/blocks/blk-1", params={"file_id": "7"}, content=b"x").status_code == 401
    assert client.get("/blocks/blk-1").status_code == 401
    assert client.delete("/blocks/blk-1").status_code == 401
    assert client.delete("/files/7/blocks").status_code == 401


def test_token_firmado_con_otro_secreto_es_401(client):
    import jwt

    forged = jwt.encode({"typ": "block", "bid": "blk-1", "op": "read"}, "otro-secreto", algorithm="HS256")
    resp = get(client, "blk-1", token=forged)
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_token"


def test_token_expirado_es_401(client):
    import datetime as dt

    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    assert get(client, "blk-1", token=read_token("blk-1", exp=past)).status_code == 401


def test_token_de_otro_bloque_es_403(client):
    put(client, "blk-1", b"secreto")
    assert get(client, "blk-1", token=read_token("blk-2")).status_code == 403


def test_token_de_lectura_no_sirve_para_escribir(client):
    resp = put(client, "blk-1", b"x", token=read_token("blk-1"))
    assert resp.status_code == 403


def test_token_de_escritura_no_sirve_para_leer(client):
    put(client, "blk-1", b"x")
    assert get(client, "blk-1", token=write_token("blk-1")).status_code == 403


def test_escritura_con_token_de_otro_archivo_es_403(client):
    resp = put(client, "blk-1", b"x", file_id="7", token=write_token("blk-1", file_id="8"))
    assert resp.status_code == 403


def test_escritura_si_este_nodo_no_esta_en_la_cadena_es_403(client):
    token = write_token("blk-1", pipe=[pipe_entry("otro-nodo")])
    assert put(client, "blk-1", b"x", token=token).status_code == 403


def test_un_token_de_usuario_no_sirve(client):
    import jwt

    from tests.conftest import TEST_SECRET

    user_like = jwt.encode({"sub": "tester", "uid": 1}, TEST_SECRET, algorithm="HS256")
    assert get(client, "blk-1", token=user_like).status_code == 401


def test_borrar_exige_token_de_controlnode(client):
    put(client, "blk-1", b"x")
    assert client.delete("/blocks/blk-1", headers=auth(write_token("blk-1"))).status_code == 401


# --------------------------------------------------------------------------
# Seguridad del storage: ids validados (sin path traversal)
# --------------------------------------------------------------------------
def test_file_id_con_path_traversal_es_400(client, tmp_path):
    for bad in ("..", "../..", "a b"):
        resp = put(client, "blk-1", b"x", file_id=bad, token=write_token("blk-1", file_id=bad))
        assert resp.status_code == 400, bad
    assert list(tmp_path.iterdir()) == []


def test_block_id_invalido_es_400(client):
    resp = put(client, "blk%2E%2E", b"x", token=write_token("blk.."))
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_id"


def test_delete_file_blocks_con_traversal_es_400_y_no_borra_nada(client, tmp_path):
    victim = tmp_path / "victima"
    victim.mkdir()
    resp = client.delete("/files/%2E%2E/blocks", headers=auth(controlnode_token()))
    assert resp.status_code == 400
    assert victim.exists()


# --------------------------------------------------------------------------
# Checksums e inmutabilidad
# --------------------------------------------------------------------------
def test_put_con_checksum_correcto_se_acepta(client):
    data = b"bloque verificado"
    resp = put(client, "blk-1", data, **{"X-Checksum-Sha256": hashlib.sha256(data).hexdigest()})
    assert resp.status_code == 200
    assert resp.json()["checksum"] == hashlib.sha256(data).hexdigest()


def test_put_con_checksum_incorrecto_se_rechaza_y_no_se_guarda(client):
    resp = put(client, "blk-1", b"contenido", **{"X-Checksum-Sha256": "0" * 64})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "checksum_mismatch"
    assert get(client, "blk-1").status_code == 404


def test_un_bloque_guardado_no_se_puede_sobrescribir(client):
    put(client, "blk-1", b"original")
    resp = put(client, "blk-1", b"otro contenido")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "block_exists"
    assert get(client, "blk-1").content == b"original"


def test_reenviar_el_mismo_contenido_es_idempotente(client):
    put(client, "blk-1", b"igual")
    assert put(client, "blk-1", b"igual").status_code == 200


def test_get_devuelve_checksum_y_tamano(client):
    data = b"x" * 200_000  # varios chunks
    put(client, "blk-1", data)
    resp = get(client, "blk-1")
    assert resp.content == data
    assert resp.headers["x-checksum-sha256"] == hashlib.sha256(data).hexdigest()
    assert resp.headers["content-length"] == str(len(data))


def test_get_de_bloque_corrupto_en_disco_es_500_y_se_informa(client, tmp_path, app_module):
    put(client, "blk-1", b"original")
    (tmp_path / "7" / "blk-1").write_bytes(b"alterado")
    resp = get(client, "blk-1")
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "corrupt_block"
    assert "blk-1" in app_module.cluster.pending_corrupt


def test_put_queda_en_cola_para_informar_al_controlnode(client, app_module):
    """Sin líder conocido, el informe de bloque recibido sale en el próximo heartbeat."""
    put(client, "blk-1", b"hola")
    assert "blk-1" in app_module.cluster.pending_received


# --------------------------------------------------------------------------
# Cadena de réplica (DataNode -> DataNode)
# --------------------------------------------------------------------------
def test_reenvia_al_siguiente_de_la_cadena(client, app_module, monkeypatch):
    calls = []

    def fake_push(block_id, file_id, token, target):
        calls.append(target["node_id"])
        return {"stored_on": [target["node_id"], "dn-c"], "failed": []}

    monkeypatch.setattr(app_module, "_push_block", fake_push)
    token = write_token("blk-1", pipe=[pipe_entry(NODE_ID), pipe_entry("dn-b"), pipe_entry("dn-c")])
    resp = put(client, "blk-1", b"replicame", token=token)
    assert resp.status_code == 200
    assert calls == ["dn-b"]  # dn-b se encarga de reenviar a dn-c
    assert resp.json()["stored_on"] == [NODE_ID, "dn-b", "dn-c"]


def test_si_el_siguiente_falla_prueba_el_de_despues(client, app_module, monkeypatch):
    import httpx

    calls = []

    def fake_push(block_id, file_id, token, target):
        calls.append(target["node_id"])
        if target["node_id"] == "dn-b":
            raise httpx.ConnectError("caído")
        return {"stored_on": [target["node_id"]], "failed": []}

    monkeypatch.setattr(app_module, "_push_block", fake_push)
    token = write_token("blk-1", pipe=[pipe_entry(NODE_ID), pipe_entry("dn-b"), pipe_entry("dn-c")])
    body = put(client, "blk-1", b"x", token=token).json()
    assert calls == ["dn-b", "dn-c"]
    assert body["stored_on"] == [NODE_ID, "dn-c"]
    assert body["failed"] == ["dn-b"]


def test_el_ultimo_de_la_cadena_no_reenvia(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_push_block", lambda *a: (_ for _ in ()).throw(AssertionError("no debía reenviar")))
    token = write_token("blk-1", pipe=[pipe_entry("dn-a"), pipe_entry(NODE_ID)])
    assert put(client, "blk-1", b"x", token=token).json()["stored_on"] == [NODE_ID]
