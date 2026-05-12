<!-- Projeto de pipelines de dados para NYC TLC Trip Records -->

# NYC TLC Trip Record Data Pipelines

**Disciplina:** Projeto Integrador
**Grupo:** H
- Caio Ribeiro
- João Albino
- Leandro Sousa
- Matheus Eiji




**Repositório:** [github.com — deng_trip_record_data](https://github.com/MathEiji/deng_trip_record_data)

---

## Sumário

- [Descrição do Problema e Objetivos](#descrição-do-problema-e-objetivos)
- [Arquitetura e Tecnologias](#arquitetura-e-tecnologias)
- [Fonte de Dados](#fonte-de-dados)
- [Organização do Repositório](#organização-do-repositório)
- [Modelo de Dados](#modelo-de-dados)
- [Instalação e Configuração](#instalação-e-configuração)
- [Execução da Aplicação](#execução-da-aplicação)
- [Infraestrutura AWS (Terraform)](#infraestrutura-aws-terraform)
- [CI/CD (GitHub Actions)](#cicd-github-actions)
- [Notebooks de Exploração](#notebooks-de-exploração)
- [Acesso aos Dados](#acesso-aos-dados)
- [Integrantes do Grupo](#integrantes-do-grupo)

---

## Descrição do Problema e Objetivos

A cidade de Nova York publica mensalmente dados abertos sobre todas as viagens realizadas por veículos de aluguel de alta demanda (FHVHV — High Volume For-Hire Vehicles), incluindo Uber, Lyft, Via e Juno. Cada arquivo mensal contém dezenas de milhões de registros (~21M linhas/mês) em formato Parquet, com informações sobre despacho, horários, localizações, tarifas e flags de solicitação.

**O problema:** os dados brutos são disponibilizados em um schema monolítico que mistura contextos distintos, dificultando a análise, a governança e a geração de insights.

**Objetivos do projeto:**
1. Construir um pipeline automatizado de **ingestão** dos dados do CDN público da TLC para o Amazon S3.
2. Implementar uma camada **raw** que normaliza o schema monolítico em tabelas contextuais, com chaves de junção e particionamento por mês.
3. Implementar uma camada **trusted** que desnormaliza, limpa e enriquece os dados com campos derivados e regras de qualidade.
4. Implementar uma camada **specialized** com agregações prontas para responder perguntas de negócio.
5. Registrar todas as tabelas no **AWS Glue Data Catalog** para consulta via Athena.
6. Orquestrar o pipeline completo com **AWS Step Functions**.
7. Automatizar o ciclo de vida com **CI/CD** (GitHub Actions) e **infraestrutura como código** (Terraform).

---

## Arquitetura e Tecnologias

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  NYC TLC CDN │────▶│  ECS Fargate     │────▶│  Amazon S3       │
│  (Parquet)   │     │  download task   │     │  staging/        │
└──────────────┘     └──────────────────┘     └────────┬─────────┘
                                                       │
                     ┌─────────────────────────────────┘
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│                    AWS Step Functions                             │
│                                                                  │
│  Download ──▶ Raw ──▶ Trusted ──▶ Specialized (4 tasks paralelo) │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│  Amazon S3                                                       │
│  ├── staging/     (dados brutos do CDN)                          │
│  ├── raw/         (tabelas normalizadas por contexto)            │
│  ├── trusted/     (dados limpos, enriquecidos, desnormalizados)  │
│  └── specialized/ (agregações para consumo)                      │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
                                   ▼
┌──────────────────┐     ┌─────────────────┐
│  AWS Glue        │────▶│  Amazon Athena   │
│  Data Catalog    │     │  (consulta SQL)  │
└──────────────────┘     └─────────────────┘
```

| Tecnologia | Uso |
|------------|-----|
| **Python 3.12** | Linguagem dos scripts de pipeline |
| **DuckDB** | Motor analítico para transformação (leitura/escrita S3 via httpfs) |
| **boto3** | SDK AWS para interação com S3 e Glue |
| **Docker** | Containerização da aplicação (imagem ARM64) |
| **ECS Fargate (Graviton/ARM64)** | Execução serverless dos containers |
| **AWS Step Functions** | Orquestração do pipeline (sequencial + paralelo) |
| **Amazon S3** | Data Lake (staging, raw, trusted, specialized) |
| **Amazon ECR** | Registry das imagens Docker |
| **AWS Glue Data Catalog** | Catálogo de metadados das tabelas |
| **Amazon Athena** | Consulta SQL serverless sobre o Data Lake |
| **CloudWatch Logs** | Logs dos containers |
| **AWS Budgets** | Alertas de custo (limite $10/mês) |
| **Terraform** | Infraestrutura como código |
| **GitHub Actions** | CI/CD — build, deploy e trigger do pipeline |

---

## Fonte de Dados

- **Origem:** [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
- **Dataset:** High Volume For-Hire Vehicle (FHVHV) Trip Records
- **Formato:** Apache Parquet (`fhvhv_tripdata_YYYY-MM.parquet`)
- **Volume típico:** ~21 milhões de linhas por mês
- **Empresas cobertas:** Uber, Lyft, Via, Juno

---

## Organização do Repositório

```
deng_trip_record_data/
├── .github/
│   └── workflows/
│       └── deploy.yml                  # CI/CD: build → deploy → pipeline
├── app/
│   ├── Dockerfile                      # Imagem Docker para todos os jobs
│   └── src/
│       ├── common/                     # Módulo compartilhado
│       │   ├── __init__.py             # Exports do módulo
│       │   ├── pipeline.py             # DuckDB, Glue, env parsing, utilities
│       │   └── specialized.py          # Framework para tabelas specialized
│       ├── download_trip_data.py       # Ingestão: CDN → S3 staging
│       ├── build_raw_layer.py          # Raw: staging → tabelas por contexto
│       ├── build_trusted_layer.py      # Trusted: raw → desnormalizado + limpo
│       ├── build_spec_hourly_volume.py # Specialized: volume por hora
│       ├── build_spec_daily_volume.py  # Specialized: volume por dia da semana
│       ├── build_spec_trip_distance.py # Specialized: distribuição de distância
│       ├── build_spec_distance_fare.py # Specialized: distância × tarifa
│       └── requirements.txt            # Dependências da aplicação
├── infra/                              # Terraform — infraestrutura AWS
│   ├── main.tf                         # Provider, backend, data sources
│   ├── variables.tf                    # Variáveis do projeto
│   ├── outputs.tf                      # Outputs (URLs, ARNs, IDs)
│   ├── backend.tf                      # Bucket S3 para Terraform state
│   ├── ecr.tf                          # Elastic Container Registry
│   ├── ecs.tf                          # ECS Cluster + 7 Task Definitions
│   ├── step_function.tf                # Step Functions state machine
│   ├── iam.tf                          # Roles (ECS, GitHub Actions, Step Functions)
│   ├── iam_users.tf                    # Usuários IAM dos desenvolvedores
│   ├── s3.tf                           # Bucket S3 para dados
│   ├── glue.tf                         # Glue Data Catalog database
│   ├── cloudwatch.tf                   # Log group do ECS
│   ├── budget.tf                       # Alertas de custo AWS
│   └── terraform.tfvars.example        # Exemplo de variáveis
├── data/
│   ├── staging/                        # Parquets baixados (git-ignored)
│   └── raw/                            # Tabelas raw locais (git-ignored)
├── reference/                          # Tabelas dimensão (versionadas)
│   ├── dim_hvfhs_license.csv           # Licença HVFHS → empresa
│   └── dim_base.csv                    # Base TLC → empresa
├── notebooks/
│   ├── data_check.ipynb                # Checagens ad-hoc
│   ├── raw_tables_exploration.ipynb    # Design das tabelas raw
│   └── requirements.txt                # Dependências dos notebooks
├── RUNBOOK.md                          # Procedimentos operacionais
├── README.md                           # Este arquivo
└── .gitignore
```

---

## Modelo de Dados

O pipeline implementa 4 camadas no Data Lake:

### Camada Staging (Bronze)

Dados brutos do CDN da TLC, sem nenhuma transformação. Parquets mensais com schema original de 24 colunas.

### Camada Raw (Silver 1)

Normalização do schema monolítico em 4 tabelas por contexto + 2 dimensões. Todas particionadas por `year_month`.

| Tabela | Contexto | Colunas principais |
|--------|----------|--------------------|
| `raw_dispatch_base` | Despacho / base | `hvfhs_license_num`, `dispatching_base_num`, `originating_base_num` |
| `raw_trip_time_location` | Tempo e localização | `request_datetime`, `pickup_datetime`, `dropoff_datetime`, `PULocationID`, `DOLocationID`, `trip_miles`, `trip_time` |
| `raw_fare_payment` | Tarifa e pagamento | `base_passenger_fare`, `tolls`, `bcf`, `sales_tax`, `congestion_surcharge`, `airport_fee`, `tips`, `driver_pay` |
| `raw_request_flags` | Flags de solicitação | `shared_request_flag`, `shared_match_flag`, `access_a_ride_flag`, `wav_request_flag`, `wav_match_flag` |

Dimensões: `dim_hvfhs_license` (licença → empresa) e `dim_base` (base TLC → empresa).

### Camada Trusted (Silver 2)

Tabela única `trusted_trips` — desnormalizada, limpa e enriquecida:

- **Join** das 4 tabelas raw + dimensão de empresa
- **Filtros de qualidade:** remove viagens com distância ≤ 0 ou > 200 mi, tempo ≤ 0 ou > 4h, tarifa ≤ 0 ou > $500, cronologia inválida
- **Campos derivados:** `wait_time_seconds`, `trip_duration_seconds`, `total_fare`, `fare_per_mile`, `pickup_hour`, `pickup_day_of_week`, `pickup_day_name`, `is_shared_request`, `is_shared_match`, `is_wav_match`
- **Particionada** por `year_month`

### Camada Specialized (Gold)

4 tabelas de agregação para responder perguntas de negócio, todas particionadas por `year_month`:

| Tabela | Pergunta de negócio | Métricas |
|--------|--------------------|-----------| 
| `spec_hourly_volume` | Quais são os horários de pico? | trip_count, avg_trip_miles, avg_total_fare, avg_duration por hora |
| `spec_daily_volume` | Quais dias da semana têm mais demanda? | trip_count, avg_trip_miles, avg_total_fare, avg_duration por dia |
| `spec_trip_distance` | Qual a distância média por empresa? | avg, median, p95, stddev, min, max de trip_miles |
| `spec_distance_fare` | Como distância se relaciona com tarifa? | avg_base_fare, avg_total_fare, avg_fare_per_mile por faixa de distância |

---

## Instalação e Configuração

### Pré-requisitos

- Python 3.12+
- Docker com Buildx (para build ARM64)
- Terraform >= 1.5
- AWS CLI v2 configurado
- Conta AWS com permissões adequadas

### Dependências Python

```bash
# Aplicação
pip install -r app/src/requirements.txt

# Notebooks (opcional)
pip install -r notebooks/requirements.txt
```

### Configuração da infraestrutura

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
# Editar terraform.tfvars com seus valores
terraform init
terraform plan
terraform apply
```

Após o `terraform apply`, configure os secrets no GitHub:

| Secret | Valor (output do Terraform) |
|--------|-----------------------------|
| `AWS_ROLE_ARN` | `github_actions_role_arn` |
| `STATE_MACHINE_ARN` | `state_machine_arn` |

> Para instruções detalhadas passo a passo, consulte o [RUNBOOK.md](RUNBOOK.md).

---

## Execução da Aplicação

### Pipeline completo (via Step Functions — recomendado)

O pipeline é orquestrado pelo AWS Step Functions na sequência:

```
Download → Raw → Trusted → Specialized (4 em paralelo)
```

Para executar via GitHub Actions:
1. Ir em **Actions → Build & Deploy → Run workflow**
2. Marcar `run_pipeline: true`
3. Informar `start_month` e `end_month`

Ou via AWS CLI:
```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"start_month": "2025-01", "end_month": "2025-12"}'
```

### Etapas individuais

Cada etapa pode ser executada isoladamente via ECS `run-task`. Consulte o [RUNBOOK.md](RUNBOOK.md) para detalhes.

---

## Infraestrutura AWS (Terraform)

Todos os recursos são definidos em `infra/` e otimizados para baixo custo:

| Recurso | Finalidade |
|---------|-----------|
| **ECR** | Registry de imagens Docker (lifecycle: últimas 5 imagens) |
| **ECS Fargate** (ARM64/Graviton) | 7 task definitions para os jobs do pipeline |
| **Step Functions** | Orquestração: sequencial + paralelo |
| **S3** | Data Lake (staging + raw + trusted + specialized) |
| **Glue Data Catalog** | Catálogo de tabelas com partições |
| **Athena** | Consulta SQL serverless |
| **CloudWatch Logs** | Logs dos containers (retenção 1 dia) |
| **AWS Budgets** | Alerta em 50%, 80% e 100% de $10/mês |
| **IAM** | Roles para ECS, Step Functions, GitHub Actions (OIDC), desenvolvedores |

O Terraform state é armazenado em um bucket S3 separado com versionamento habilitado.

---

## CI/CD (GitHub Actions)

O workflow `.github/workflows/deploy.yml` é acionado em pushes para `main` que alteram `app/`, `reference/` ou o próprio workflow:

1. **build-and-push** — Builda a imagem Docker ARM64 e publica no ECR
2. **deploy** — Atualiza as 7 task definitions do ECS com a nova imagem
3. **run-pipeline** *(manual via workflow_dispatch)* — Dispara o Step Functions e aguarda conclusão

A autenticação com a AWS usa **OIDC** (OpenID Connect), sem credenciais estáticas.

---

## Notebooks de Exploração

| Notebook | Descrição |
|----------|-----------|
| `notebooks/data_check.ipynb` | Checagens ad-hoc: contagens, amostragem, validações com DuckDB e pandas |
| `notebooks/raw_tables_exploration.ipynb` | Design das tabelas raw: inspeção de schema, análise de contexto |

```bash
pip install -r notebooks/requirements.txt
jupyter notebook notebooks/
```

---

## Acesso aos Dados

### Via Athena

Após a execução do pipeline, todas as tabelas ficam disponíveis no Glue Data Catalog (database `trip_record_data`):

```sql
-- Volume por hora (horários de pico)
SELECT * FROM trip_record_data.spec_hourly_volume
WHERE year_month = 202506;

-- Distância × tarifa
SELECT * FROM trip_record_data.spec_distance_fare
WHERE year_month = 202506;

-- Dados trusted completos
SELECT * FROM trip_record_data.trusted_trips
WHERE year_month = 202506
LIMIT 100;
```

### Via DuckDB (local)

```python
import duckdb
con = duckdb.connect()
con.execute("SELECT * FROM read_parquet('data/raw/raw_fare_payment/**/*.parquet', hive_partitioning=true) LIMIT 10").fetchdf()
```

---

## Integrantes do Grupo

| Nome | Usuário IAM |
|------|-------------|
| Matheus Eiji | — |
| Leandro Sousa | `leandro.sousa` |
| Caio Ribeiro | `caio.ribeiro` |
| João Albino | `joao.albino` |
