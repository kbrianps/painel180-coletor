#!/usr/bin/env python3
"""Coleta o Painel da Rede de Atendimento (Ligue 180) direto da API do Power BI."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PAINEL_URL = "https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento"
RESOURCE_KEY = "a1b972db-9924-4fcd-b04a-fd619fb27007"
API = "https://wabi-brazil-south-d-primary-api.analysis.windows.net/public/reports"
VISUAL_ID = 230683235
DESTINO = Path(os.environ.get("PAINEL180_DIR", Path.home() / "painel180" / "dados"))
MANTER = int(os.environ.get("PAINEL180_MANTER", "5"))
UF_ALVO = os.environ.get("PAINEL180_UF", "RJ").strip().upper()  # vazio = Brasil inteiro
TIMEOUT = 60

COLUNAS_CSV = {
    "dEndereço.UF": "UF",
    "dEndereço.Municipio": "Municipio",
    "dEndereço.Municipio + UF": "Municipio_UF",
    "dEndereço.Estado": "Estado",
    "dEndereço.Unidade Federativa": "Unidade_Federativa",
    "dEndereço.Regiao": "Regiao",
    "dEndereço.CEP": "CEP",
    "dEndereço.Endereco": "Endereco",
    "dEndereço.Bairro": "Bairro",
    "dEndereço.Combinação Endereço": "Endereco_Completo",
    "dEndereço.Latitude": "Latitude",
    "dEndereço.Longitude": "Longitude",
    "dServico.Tipo Serviço": "Tipo_Servico",
    "dServico.Nome do Serviço": "Nome_Servico",
    "dServico.Telefone": "Telefone",
    "dServico.OutroTelefone": "Outro_Telefone",
    "dServico.Email": "Email",
    "dServico.Site": "Site",
    "dServico.RedeSocial": "Rede_Social",
}


def cabecalhos() -> dict[str, str]:
    return {
        "ActivityId": str(uuid.uuid4()),
        "RequestId": str(uuid.uuid4()),
        "X-PowerBI-ResourceKey": RESOURCE_KEY,
        "Origin": "https://app.powerbi.com",
        "Referer": "https://app.powerbi.com/",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) painel180-coletor",
    }


def pedir(url: str, corpo: bytes | None = None) -> dict:
    cabec = cabecalhos()
    if corpo is not None:
        cabec["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(url, data=corpo, headers=cabec, method="POST" if corpo else "GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        dados = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip" or dados[:2] == b"\x1f\x8b":
            dados = gzip.decompress(dados)
        return json.loads(dados.decode("utf-8"))


CAMPOS = [
    ("d", "dEndereço", ["UF", "Municipio", "Municipio + UF", "Estado", "Unidade Federativa",
                        "Regiao", "CEP", "Endereco", "Bairro", "Combinação Endereço",
                        "Latitude", "Longitude"]),
    ("s", "dServico", ["Tipo Serviço", "Nome do Serviço", "Telefone", "OutroTelefone",
                       "Email", "Site", "RedeSocial"]),
]


def montar_consulta() -> dict:
    """Monta a consulta direto sobre o modelo, em vez de copiar a de um visual.

    Visuais somem ou mudam quando o painel e redesenhado; os campos do modelo nao."""
    selecao = []
    for origem, entidade, propriedades in CAMPOS:
        for prop in propriedades:
            selecao.append({
                "Column": {"Expression": {"SourceRef": {"Source": origem}}, "Property": prop},
                "Name": f"{entidade}.{prop}",
            })
    return {"Commands": [{"SemanticQueryDataShapeCommand": {
        "Query": {
            "Version": 2,
            "From": [{"Name": o, "Entity": e, "Type": 0} for o, e, _ in CAMPOS],
            "Select": selecao,
        },
        "Binding": {
            "Primary": {"Groupings": [{"Projections": list(range(len(selecao)))}]},
            "DataReduction": {"DataVolume": 6, "Primary": {"Window": {"Count": 30000}}},
            "Version": 1,
        },
    }}]}


def dados_do_modelo(modelo: dict) -> dict:
    mod = (modelo.get("models") or [{}])[0]
    bruto = mod.get("lastRefreshTime") or ""
    atualizado = bruto
    achado = re.search(r"/Date\((\d+)\)/", str(bruto))
    if achado:
        atualizado = datetime.fromtimestamp(int(achado.group(1)) / 1000, timezone.utc).isoformat()
    return {
        "datasetId": mod.get("dbName") or mod.get("datasetId"),
        "modelId": mod.get("id"),
        "lastRefresh": atualizado,
    }


def ler_linhas(resposta: dict) -> tuple[list[str], list[list]]:
    dados = resposta["results"][0]["result"]["data"]
    colunas = [s["Name"] for s in dados["descriptor"]["Select"]]
    ds = dados["dsr"]["DS"][0]
    dicionarios = ds.get("ValueDicts", {})
    linhas_brutas = ds["PH"][0]["DM0"]

    mapa_dict = {}
    for i, campo in enumerate(linhas_brutas[0].get("S", [])):
        if campo.get("DN"):
            mapa_dict[i] = campo["DN"]

    linhas: list[list] = []
    anterior: list = [None] * len(colunas)
    for bruta in linhas_brutas:
        valores_crus = bruta.get("C", [])
        repete = bruta.get("R", 0)
        nulos = bruta.get("Ø", 0)
        linha: list = []
        pos = 0
        for i in range(len(colunas)):
            if nulos >> i & 1:
                valor = None
            elif repete >> i & 1:
                valor = anterior[i]
            else:
                valor = valores_crus[pos] if pos < len(valores_crus) else None
                pos += 1
                if i in mapa_dict and isinstance(valor, int):
                    tabela = dicionarios.get(mapa_dict[i], [])
                    if 0 <= valor < len(tabela):
                        valor = tabela[valor]
            linha.append(valor)
        anterior = linha
        linhas.append(linha)
    return colunas, linhas


def gravar(destino: Path, colunas: list[str], linhas: list[list], modelo_info: dict) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    bruto = {
        "sourceUrl": PAINEL_URL,
        "reportName": "Painel da Rede de Atendimento à Mulher",
        "datasetId": modelo_info.get("datasetId"),
        "modelId": modelo_info.get("modelId"),
        "lastRefresh": modelo_info.get("lastRefresh"),
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "columns": colunas,
        "uf": UF_ALVO or "BR",
        "rowCount": len(linhas),
        "rows": linhas,
    }
    (destino / "painel_bruto.json").write_text(
        json.dumps(bruto, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    nomes = [COLUNAS_CSV.get(c, c.replace(".", "_").replace(" ", "_")) for c in colunas]
    with (destino / "painel_servicos.csv").open("w", encoding="utf-8", newline="") as fh:
        escritor = csv.writer(fh)
        escritor.writerow(nomes + ["Fonte_URL", "Data_Extracao", "Ultima_Atualizacao_Modelo"])
        for linha in linhas:
            escritor.writerow(
                ["" if v is None else v for v in linha]
                + [PAINEL_URL, bruto["extractedAt"], bruto["lastRefresh"]]
            )


def podar(base: Path, manter: int) -> list[str]:
    versoes = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
    removidas = []
    for velha in versoes[manter:]:
        shutil.rmtree(velha, ignore_errors=True)
        removidas.append(velha.name)
    return removidas


def main() -> int:
    modelo = pedir(f"{API}/{RESOURCE_KEY}/modelsAndExploration?preferReadOnlySession=true")
    info = dados_do_modelo(modelo)

    corpo = {
        "version": "1.0.0",
        "queries": [{
            "Query": montar_consulta(),
            "ApplicationContext": {
                "DatasetId": info["datasetId"],
                "Sources": [{"ReportId": RESOURCE_KEY}],
            },
        }],
        "cancelQueries": [],
        "modelId": info["modelId"],
    }
    resposta = pedir(
        f"{API}/querydata?synchronous=true", json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    )
    colunas, linhas = ler_linhas(resposta)
    if len(linhas) < 100:
        print(f"ATENCAO: so {len(linhas)} linhas - possivel mudanca na API. Nada foi gravado.")
        return 1

    total_brasil = len(linhas)
    if UF_ALVO:
        try:
            coluna_uf = colunas.index("dEndereço.UF")
        except ValueError:
            print("ATENCAO: coluna de UF sumiu da resposta. Nada foi gravado.")
            return 1
        linhas = [l for l in linhas if str(l[coluna_uf] or "").strip().upper() == UF_ALVO]
        if not linhas:
            print(f"ATENCAO: nenhum servico em {UF_ALVO} entre {total_brasil}. Nada foi gravado.")
            return 1

    impressao = hashlib.sha256(
        json.dumps(linhas, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    base = DESTINO
    base.mkdir(parents=True, exist_ok=True)
    marca = base / "ultima_impressao.txt"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    if marca.exists() and marca.read_text().strip() == impressao:
        print(f"[{agora}] sem mudanca ({len(linhas)} servicos em {UF_ALVO or 'BR'}, {total_brasil} no Brasil)")
        return 0

    destino = base / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    gravar(destino, colunas, linhas, info)
    marca.write_text(impressao)
    removidas = podar(base, MANTER)
    print(
        f"[{agora}] MUDOU: {len(linhas)} servicos em {UF_ALVO or 'BR'} (de {total_brasil} no Brasil) -> {destino.name}"
        + (f" | removidas: {', '.join(removidas)}" if removidas else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
