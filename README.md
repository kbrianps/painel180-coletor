# Coletor do Painel da Rede de Atendimento (Ligue 180)

Coleta periodicamente os serviços da rede de atendimento à mulher publicados no
[Painel da Rede de Atendimento](https://www.gov.br/mulheres/pt-br/ligue180/painel-da-rede-de-atendimento)
do Ministério das Mulheres, e guarda um histórico das versões que mudaram.

## Como os dados são obtidos

O painel é um relatório **Power BI** público embutido na página do gov.br. Não há raspagem de
HTML: o script conversa com a API pública do Power BI em dois passos.

1. `GET .../modelsAndExploration` devolve o modelo de dados do relatório (dataset, modelo e data
   da última atualização).
2. `POST .../querydata` executa uma consulta semântica sobre esse modelo e devolve as linhas.

A consulta é **montada pelo script** sobre as entidades `dEndereço` e `dServico`, em vez de copiar
a consulta de algum gráfico do painel. Gráficos somem ou mudam de identificador quando o painel é
redesenhado; os campos do modelo são estáveis.

A resposta vem comprimida: valores repetidos viram uma máscara de bits (`R`), nulos viram outra
(`Ø`) e textos viram índices para dicionários por coluna (`ValueDicts`). O script desfaz essa
compressão em `ler_linhas`.

## O que ele grava

Cada coleta que **muda** o conteúdo grava:

| Caminho | Conteúdo |
|---|---|
| `painel_servicos.csv` | um serviço por linha, 19 campos, mais a fonte e as datas — Brasil inteiro |
| `painel_bruto.json` | as mesmas linhas sem formatação, com metadados do modelo |
| `uf/<SIGLA>/…` | os mesmos dois arquivos, recortados para um estado (só com `--uf`) |

O arquivo nacional é sempre gravado. O recorte por estado é adicional, para quem acompanha uma
região específica sem perder o retrato do país.

Quando nada muda, nada é gravado: o script compara um `sha256` das linhas com o valor em
`ultima_impressao.txt` e apenas registra "sem mudança" no log. Isso importa porque o painel é
atualizado de forma esporádica — entre 12/08/2026 e 19/09/2026 os dados não mudaram uma vez.

Sem `--repo`, cada mudança vira uma pasta com data e as mais antigas são apagadas, mantendo as
`--manter` mais recentes.

### Modo repositório

Com `--repo` apontando para um clone de um repositório Git, o script grava sempre nos mesmos
caminhos dentro de `dados/` e faz commit e push a cada mudança. O histórico do Git passa a ser o
histórico do painel: cada commit é uma mudança real, e o diff mostra quais serviços entraram,
saíram ou tiveram dados corrigidos. Nesse modo não há rotação, e o número de versões é ilimitado.

Numa máquina sem supervisão, use uma chave de implantação com escrita restrita ao repositório, em
vez de uma credencial da sua conta inteira.

## Configuração

Todas as opções têm uma variável de ambiente equivalente, o que facilita usar com systemd:

| Opção | Variável | Padrão | Função |
|---|---|---|---|
| `--uf` | `PAINEL180_UF` | vazio | recorte extra de um estado (ex.: `RJ`) |
| `--dir` | `PAINEL180_DIR` | `~/painel180/dados` | onde gravar |
| `--repo` | `PAINEL180_REPO` | vazio | clone Git onde gravar e comitar |
| `--manter` | `PAINEL180_MANTER` | `5` | versões mantidas quando não se usa `--repo` |
| `--minimo` | `PAINEL180_MINIMO` | `100` | mínimo de linhas aceitável |
| `--resource-key` | `PAINEL180_RESOURCE_KEY` | o do painel | identificador do relatório Power BI |
| `--api` | `PAINEL180_API` | Brasil Sul | endereço da API, que varia com a região |
| `--timeout` | `PAINEL180_TIMEOUT` | `60` | tempo limite por requisição, em segundos |

E três chaves sem variável: `--simular` coleta e mostra o resumo sem gravar, `--forcar` grava
mesmo sem mudança, e `--silencioso` imprime apenas erros.

```bash
python3 coleta_painel180.py --simular            # ver o que viria, sem gravar
python3 coleta_painel180.py --uf BA              # Brasil, com recorte da Bahia
python3 coleta_painel180.py --repo ~/meu-clone   # commit automático a cada mudança
```

Como `--resource-key` e `--api` são configuráveis, o script serve para qualquer relatório Power BI
público de estrutura parecida, bastando ajustar `CAMPOS` com as entidades e propriedades do outro
modelo.

## Salvaguardas

O script se recusa a gravar, e sai com erro, se:

- vierem menos de 100 serviços no total do Brasil (indica mudança ou falha na API);
- a coluna de UF desaparecer da resposta;
- o estado escolhido ficar sem nenhum serviço.

Em qualquer um desses casos os dados já coletados permanecem intactos.

## Instalação

```bash
mkdir -p ~/painel180 && cp coleta_painel180.py ~/painel180/
sudo cp systemd/painel180.service systemd/painel180.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now painel180.timer
```

Só precisa de Python 3 — nenhuma biblioteca externa.

Para rodar uma vez, à mão:

```bash
python3 ~/painel180/coleta_painel180.py
```

Para acompanhar:

```bash
systemctl list-timers painel180.timer
journalctl -u painel180.service -n 20
```

## Origem

Os dados equivalentes foram levantados pela primeira vez em 24/08/2026, de forma manual, apenas
para o Rio de Janeiro (180 serviços). Este script reproduz exatamente aquele resultado — as 180
linhas conferem campo a campo — e amplia o alcance para os 2.641 serviços do país.
