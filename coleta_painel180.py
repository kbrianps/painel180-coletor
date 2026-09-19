#!/usr/bin/env python3
"""Coleta o Painel da Rede de Atendimento (Ligue 180) direto da API do Power BI.

O painel do Ministerio das Mulheres e um relatorio Power BI publico. Este script
conversa com a API publica do Power BI, descomprime a resposta e grava CSV e JSON.

Uso rapido:
    python3 coleta_painel180.py                 # Brasil inteiro
    python3 coleta_painel180.py --uf RJ         # Brasil + um recorte do estado
    python3 coleta_painel180.py --repo ~/clone  # grava e comita num repositorio

Toda opcao tem uma variavel de ambiente equivalente (PAINEL180_*), util em systemd.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

PAINEL_URL = "https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento"
RESOURCE_KEY_PADRAO = "a1b972db-9924-4fcd-b04a-fd619fb27007"
API_PADRAO = "https://wabi-brazil-south-d-primary-api.analysis.windows.net/public/reports"
NOME_RELATORIO = "Painel da Rede de Atendimento à Mulher"

# Campos pedidos ao modelo: (apelido, entidade, propriedades).
CAMPOS = [
    ("d", "dEndereço", ["UF", "Municipio", "Municipio + UF", "Estado", "Unidade Federativa",
                        "Regiao", "CEP", "Endereco", "Bairro", "Combinação Endereço",
                        "Latitude", "Longitude"]),
    ("s", "dServico", ["Tipo Serviço", "Nome do Serviço", "Telefone", "OutroTelefone",
                       "Email", "Site", "RedeSocial"]),
]

# Nomes amigaveis no CSV. Campos fora daqui recebem um nome derivado automaticamente.
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

COLUNA_UF = "dEndereço.UF"
UFS_VALIDAS = {
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT",
    "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO",
}


# ----------------------------------------------------------------------- opcoes


def ambiente(nome: str, padrao: str = "") -> str:
    return os.environ.get(f"PAINEL180_{nome}", padrao).strip()


def opcoes(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Coleta o Painel da Rede de Atendimento (Ligue 180).",
        epilog="Cada opcao aceita a variavel de ambiente equivalente: PAINEL180_UF, "
               "PAINEL180_DIR, PAINEL180_REPO, PAINEL180_MANTER, PAINEL180_MINIMO, "
               "PAINEL180_RESOURCE_KEY, PAINEL180_API, PAINEL180_TIMEOUT.",
    )
    p.add_argument("--uf", default=ambiente("UF"),
                   help="sigla de um estado (ex.: RJ). Alem do arquivo nacional, grava um "
                        "recorte so desse estado. Padrao: apenas o nacional.")
    p.add_argument("--dir", dest="destino", default=ambiente("DIR"),
                   help="pasta onde gravar (padrao: ~/painel180/dados).")
    p.add_argument("--repo", default=ambiente("REPO"),
                   help="clone de um repositorio Git: grava em <repo>/dados e comita a cada "
                        "mudanca, usando o historico do Git no lugar da rotacao de pastas.")
    p.add_argument("--manter", type=int, default=int(ambiente("MANTER", "5") or 5),
                   help="quantas versoes manter quando nao se usa --repo (padrao: 5).")
    p.add_argument("--minimo", type=int, default=int(ambiente("MINIMO", "100") or 100),
                   help="recusa gravar se vierem menos linhas que isto (padrao: 100).")
    p.add_argument("--resource-key", default=ambiente("RESOURCE_KEY", RESOURCE_KEY_PADRAO),
                   help="identificador do relatorio Power BI.")
    p.add_argument("--api", default=ambiente("API", API_PADRAO),
                   help="endereco da API do Power BI (muda conforme a regiao do relatorio).")
    p.add_argument("--timeout", type=int, default=int(ambiente("TIMEOUT", "60") or 60),
                   help="tempo limite de cada requisicao, em segundos (padrao: 60).")
    p.add_argument("--forcar", action="store_true",
                   help="grava mesmo que o conteudo nao tenha mudado.")
    p.add_argument("--simular", action="store_true",
                   help="coleta e mostra o resumo, sem gravar nada.")
    p.add_argument("--silencioso", action="store_true", help="so imprime erros.")
    op = p.parse_args(argv)

    op.uf = op.uf.strip().upper()
    if op.uf and op.uf not in UFS_VALIDAS:
        p.error(f"UF invalida: {op.uf}. Use uma das 27 siglas ou deixe vazio.")
    op.destino = Path(op.destino) if op.destino else Path.home() / "painel180" / "dados"
    return op


# -------------------------------------------------------------------------- api


def pedir(url: str, corpo: bytes | None, resource_key: str, timeout: int) -> dict:
    cabec = {
        "ActivityId": str(uuid.uuid4()),
        "RequestId": str(uuid.uuid4()),
        "X-PowerBI-ResourceKey": resource_key,
        "Origin": "https://app.powerbi.com",
        "Referer": "https://app.powerbi.com/",
        "Accept": "application/json",
        "User-Agent": "painel180-coletor (https://github.com/kbrianps/painel180-coletor)",
    }
    if corpo is not None:
        cabec["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(url, data=corpo, headers=cabec,
                                 method="POST" if corpo else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dados = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip" or dados[:2] == b"\x1f\x8b":
            dados = gzip.decompress(dados)
        return json.loads(dados.decode("utf-8"))


def montar_consulta() -> dict:
    """Monta a consulta sobre o modelo, em vez de copiar a de um visual do painel.

    Visuais somem ou mudam de identificador quando o painel e redesenhado; os campos
    do modelo sao estaveis. O visual usado na coleta manual de 2026-08 ja nao existe."""
    selecao = [
        {"Column": {"Expression": {"SourceRef": {"Source": origem}}, "Property": prop},
         "Name": f"{entidade}.{prop}"}
        for origem, entidade, propriedades in CAMPOS
        for prop in propriedades
    ]
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
    bruto = str(mod.get("lastRefreshTime") or "")
    achado = re.search(r"/Date\((\d+)\)/", bruto)
    if achado:
        bruto = datetime.fromtimestamp(int(achado.group(1)) / 1000, timezone.utc).isoformat()
    return {"datasetId": mod.get("dbName") or mod.get("datasetId"),
            "modelId": mod.get("id"), "lastRefresh": bruto}


def coletar(op: argparse.Namespace) -> tuple[list[str], list[list], dict]:
    modelo = pedir(f"{op.api}/{op.resource_key}/modelsAndExploration?preferReadOnlySession=true",
                   None, op.resource_key, op.timeout)
    info = dados_do_modelo(modelo)
    corpo = {
        "version": "1.0.0",
        "queries": [{"Query": montar_consulta(),
                     "ApplicationContext": {"DatasetId": info["datasetId"],
                                            "Sources": [{"ReportId": op.resource_key}]}}],
        "cancelQueries": [],
        "modelId": info["modelId"],
    }
    resposta = pedir(f"{op.api}/querydata?synchronous=true",
                     json.dumps(corpo, ensure_ascii=False).encode("utf-8"),
                     op.resource_key, op.timeout)
    colunas, linhas = ler_linhas(resposta)
    return colunas, linhas, info


def ler_linhas(resposta: dict) -> tuple[list[str], list[list]]:
    """Desfaz a compressao do formato DSR do Power BI.

    Cada linha traz so os valores que mudaram: `R` e uma mascara de bits que manda
    repetir o valor da linha anterior, `Ø` marca nulos, e textos vem como indices
    para dicionarios por coluna em `ValueDicts`."""
    dados = resposta["results"][0]["result"]["data"]
    colunas = [s["Name"] for s in dados["descriptor"]["Select"]]
    ds = dados["dsr"]["DS"][0]
    dicionarios = ds.get("ValueDicts", {})
    linhas_brutas = ds["PH"][0]["DM0"]

    mapa_dict = {i: campo["DN"]
                 for i, campo in enumerate(linhas_brutas[0].get("S", []))
                 if campo.get("DN")}

    linhas: list[list] = []
    anterior: list = [None] * len(colunas)
    for bruta in linhas_brutas:
        valores = bruta.get("C", [])
        repete, nulos = bruta.get("R", 0), bruta.get("Ø", 0)
        linha: list = []
        pos = 0
        for i in range(len(colunas)):
            if nulos >> i & 1:
                valor = None
            elif repete >> i & 1:
                valor = anterior[i]
            else:
                valor = valores[pos] if pos < len(valores) else None
                pos += 1
                if i in mapa_dict and isinstance(valor, int):
                    tabela = dicionarios.get(mapa_dict[i], [])
                    if 0 <= valor < len(tabela):
                        valor = tabela[valor]
            linha.append(valor)
        anterior = linha
        linhas.append(linha)
    return colunas, linhas


# ------------------------------------------------------------------------ saida


def gravar(destino: Path, colunas: list[str], linhas: list[list], info: dict,
           rotulo: str, total_brasil: int) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    bruto = {
        "sourceUrl": PAINEL_URL,
        "reportName": NOME_RELATORIO,
        "datasetId": info.get("datasetId"),
        "modelId": info.get("modelId"),
        "lastRefresh": info.get("lastRefresh"),
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "uf": rotulo,
        "rowCountBrasil": total_brasil,
        "columns": colunas,
        "rowCount": len(linhas),
        "rows": linhas,
    }
    (destino / "painel_bruto.json").write_text(
        json.dumps(bruto, ensure_ascii=False, indent=2), encoding="utf-8")

    nomes = [COLUNAS_CSV.get(c, c.split(".")[-1].replace(" ", "_")) for c in colunas]
    with (destino / "painel_servicos.csv").open("w", encoding="utf-8", newline="") as fh:
        escritor = csv.writer(fh)
        escritor.writerow(nomes + ["Fonte_URL", "Data_Extracao", "Ultima_Atualizacao_Modelo"])
        for linha in linhas:
            escritor.writerow(["" if v is None else v for v in linha]
                              + [PAINEL_URL, bruto["extractedAt"], bruto["lastRefresh"]])


def filtrar_uf(colunas: list[str], linhas: list[list], uf: str) -> list[list]:
    if COLUNA_UF not in colunas:
        return []
    idx = colunas.index(COLUNA_UF)
    return [l for l in linhas if str(l[idx] or "").strip().upper() == uf]


def publicar_no_git(repo: Path, resumo: str) -> str:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=120)

    git("add", "dados")
    if not git("status", "--porcelain", "dados").stdout.strip():
        return "sem alteracao para comitar"
    feito = git("commit", "-m", resumo)
    if feito.returncode != 0:
        return f"falha no commit: {feito.stderr.strip()[:200]}"
    enviado = git("push")
    if enviado.returncode != 0:
        return f"commit feito, push falhou: {enviado.stderr.strip()[:200]}"
    return "commit e push ok"


def podar(base: Path, manter: int) -> list[str]:
    versoes = sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)
    removidas = []
    for velha in versoes[manter:]:
        shutil.rmtree(velha, ignore_errors=True)
        removidas.append(velha.name)
    return removidas


# ------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    op = opcoes(argv)
    fala = (lambda *a: None) if op.silencioso else print

    try:
        colunas, linhas, info = coletar(op)
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as erro:
        print(f"ERRO ao coletar: {type(erro).__name__}: {erro}", file=sys.stderr)
        return 2

    total_brasil = len(linhas)
    if total_brasil < op.minimo:
        print(f"ERRO: so {total_brasil} linhas no Brasil (minimo {op.minimo})."
              " Possivel mudanca na API. Nada foi gravado.", file=sys.stderr)
        return 1

    linhas_uf: list[list] = []
    if op.uf:
        linhas_uf = filtrar_uf(colunas, linhas, op.uf)
        if not linhas_uf:
            print(f"ERRO: nenhum servico em {op.uf} entre {total_brasil}."
                  " Nada foi gravado.", file=sys.stderr)
            return 1

    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    resumo = f"{total_brasil} servicos no Brasil"
    if op.uf:
        resumo += f", {len(linhas_uf)} em {op.uf}"

    if op.simular:
        fala(f"[{agora}] simulacao: {resumo}"
             f" | modelo atualizado em {(info.get('lastRefresh') or '?')[:10]}")
        return 0

    impressao = hashlib.sha256(
        json.dumps(linhas, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    base = Path(op.repo) / "dados" if op.repo else op.destino
    base.mkdir(parents=True, exist_ok=True)
    marca = base / "ultima_impressao.txt"

    if not op.forcar and marca.exists() and marca.read_text().strip() == impressao:
        fala(f"[{agora}] sem mudanca ({resumo})")
        return 0

    alvo = base if op.repo else op.destino / datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H-%M-%SZ")
    gravar(alvo, colunas, linhas, info, "BR", total_brasil)
    if op.uf:
        gravar(alvo / "uf" / op.uf, colunas, linhas_uf, info, op.uf, total_brasil)
    marca.write_text(impressao)

    if op.repo:
        rotulo = f"Brasil: {total_brasil} servicos" + (
            f" | {op.uf}: {len(linhas_uf)}" if op.uf else "")
        estado = publicar_no_git(
            Path(op.repo),
            f"{rotulo} (modelo atualizado em {(info.get('lastRefresh') or '?')[:10]})")
        fala(f"[{agora}] MUDOU: {resumo} | {estado}")
        return 0

    removidas = podar(op.destino, op.manter)
    fala(f"[{agora}] MUDOU: {resumo} -> {alvo.name}"
         + (f" | removidas: {', '.join(removidas)}" if removidas else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
