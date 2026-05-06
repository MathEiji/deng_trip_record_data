# RUNBOOK — NYC TLC Trip Record Data Pipelines

Procedimentos operacionais para preparação, execução, validação e manutenção do pipeline de dados.

---

## Sumário

- [Pré-requisitos](#pré-requisitos)
- [1. Preparação do Ambiente Local](#1-preparação-do-ambiente-local)
- [2. Provisionamento da Infraestrutura (Terraform)](#2-provisionamento-da-infraestrutura-terraform)
- [3. Configuração do CI/CD (GitHub Actions)](#3-configuração-do-cicd-github-actions)
- [4. Build e Push da Imagem Docker](#4-build-e-push-da-imagem-docker)
- [5. Execução do Pipeline Completo (Step Functions)](#5-execução-do-pipeline-completo-step-functions)
- [6. Execução de Etapas Individuais](#6-execução-de-etapas-individuais)
- [7. Validação e Testes](#7-validação-e-testes)
- [8. Reexecução e Reprocessamento](#8-reexecução-e-reprocessamento)
- [9. Problemas Conhecidos e Limitações](#9-problemas-conhecidos-e-limitações)
- [10. Ações de Contingência](#10-ações-de-contingência)

---

## Pré-requisitos

### Ferramentas

| Ferramenta | Versão mínima | Finalidade |
|------------|---------------|-----------|
| Python | 3.12+ | Execução dos scripts |
| pip | — | Gerenciamento de dependências Python |
| Docker | 20.10+ | Build da imagem do container |
| Docker Buildx | — | Build multi-plataforma (ARM64) |
| Terraform | >= 1.5 | Provisionamento da infraestrutura AWS |
| AWS CLI | v2 | Interação com serviços AWS |
| Git | — | Controle de versão |

### Contas e Acessos

- **Conta AWS** com permissões para criar: S3, ECR, ECS, IAM, Glue, Step Functions, CloudWatch, Budgets
- **Repositório GitHub** com permissão para configurar secrets e GitHub Actions
- **AWS CLI configurado** com credenciais válidas (`aws configure`)

### Variáveis de Ambiente (referência)

| Variável | Obrigatória | Descrição | Padrão |
|----------|-------------|-----------|--------|
| `S3_BUCKET` | Sim | Nome do bucket S3 para dados | — |
| `START_MONTH` | Sim | Mês inicial no formato `YYYY-MM` | — |
| `END_MONTH` | Sim | Mês final no formato `YYYY-MM` | — |
| `S3_PREFIX` | Não | Prefixo para staging (download) | `staging` |
| `S3_STAGING_PREFIX` | Não | Prefixo de staging (build raw) | `staging` |
| `S3_RAW_PREFIX` | Não | Prefixo de saída raw | `raw` |
| `S3_TRUSTED_PREFIX` | Não | Prefixo de saída trusted | `trusted` |
| `S3_SPECIALIZED_PREFIX` | Não | Prefixo de saída specialized | `specialized` |
| `GLUE_DATABASE` | Não | Nome do database no Glue Catalog | `trip_record_data` |
| `AWS_REGION` | Não | Região AWS | `us-east-1` |
| `SKIP_QUALITY_ANALYSIS` | Não | Pular análise de qualidade (raw) | `false` |

---

## 1. Preparação do Ambiente Local

### 1.1 Clonar o repositório

```bash
git clone https://github.com/MathEiji/deng_trip_record_data.git
cd deng_trip_record_data
```

### 1.2 Criar ambiente virtual Python

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 1.3 Instalar dependências da aplicação

```bash
pip install -r app/src/requirements.txt
```

### 1.4 Instalar dependências dos notebooks (opcional)

```bash
pip install -r notebooks/requirements.txt
```

### 1.5 Verificar AWS CLI

```bash
aws sts get-caller-identity
```

Deve retornar o ARN da sua conta/role. Caso contrário, configure com `aws configure`.

---

## 2. Provisionamento da Infraestrutura (Terraform)

### 2.1 Configurar variáveis

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

Editar `terraform.tfvars` com os valores do seu ambiente:

```hcl
aws_region     = "us-east-1"
project_name   = "trip-record-data"
github_org     = "seu-usuario-github"
github_repo    = "deng_trip_record_data"
s3_bucket_name = "nome-unico-global-do-bucket"
start_month    = "2025-01"
end_month      = "2025-12"
glue_database  = "trip_record_data"
alert_emails   = ["email@exemplo.com"]
```

> **Importante:** o `s3_bucket_name` deve ser globalmente único na AWS.

### 2.2 Inicializar e aplicar

```bash
terraform init
terraform plan    # Revisar os recursos que serão criados
terraform apply   # Confirmar com "yes"
```

### 2.3 Anotar os outputs

```bash
terraform output
```

Outputs relevantes:

| Output | Uso |
|--------|-----|
| `ecr_repository_url` | URL do ECR para push de imagens |
| `ecs_cluster_name` | Nome do cluster ECS |
| `s3_bucket_name` | Bucket de dados |
| `github_actions_role_arn` | ARN da role para GitHub Actions |
| `state_machine_arn` | ARN do Step Functions |
| `security_group_id` | SG para tasks ECS |
| `subnet_ids` | Subnets para tasks ECS |

### 2.4 Recursos criados

- Bucket S3 para dados (staging + raw + trusted + specialized)
- Bucket S3 para Terraform state (com versionamento)
- ECR repository com lifecycle policy (mantém últimas 5 imagens)
- ECS Cluster + 7 Task Definitions:
  - `trip-record-data-download` (0.25 vCPU / 512 MB)
  - `trip-record-data-build-raw` (1 vCPU / 4 GB / 21 GB ephemeral)
  - `trip-record-data-build-trusted` (1 vCPU / 4 GB / 21 GB ephemeral)
  - `trip-record-data-build-spec-hourly-volume` (0.5 vCPU / 2 GB)
  - `trip-record-data-build-spec-daily-volume` (0.5 vCPU / 2 GB)
  - `trip-record-data-build-spec-trip-distance` (0.5 vCPU / 2 GB)
  - `trip-record-data-build-spec-distance-fare` (0.5 vCPU / 2 GB)
- Step Functions state machine (download → raw → trusted → specialized em paralelo)
- Security Group (egress-only)
- IAM Roles: ECS execution, ECS task, GitHub Actions (OIDC), Step Functions
- IAM Users para desenvolvedores (leandro.sousa, caio.ribeiro, joao.albino)
- Glue Data Catalog database
- CloudWatch Log Group (retenção 1 dia)
- AWS Budgets (alertas em 50%, 80%, 100% de $10/mês)

---

## 3. Configuração do CI/CD (GitHub Actions)

### 3.1 Configurar secrets no GitHub

No repositório GitHub, ir em **Settings → Secrets and variables → Actions** e criar:

| Secret | Valor |
|--------|-------|
| `AWS_ROLE_ARN` | Valor do output `github_actions_role_arn` |
| `STATE_MACHINE_ARN` | Valor do output `state_machine_arn` |

### 3.2 Trigger automático

O workflow é acionado automaticamente em pushes para `main` que alteram:
- `app/**`
- `reference/**`
- `.github/workflows/deploy.yml`

### 3.3 Trigger manual (workflow_dispatch)

No GitHub, ir em **Actions → Build & Deploy → Run workflow** e configurar:

| Parâmetro | Tipo | Descrição |
|-----------|------|-----------|
| `run_pipeline` | boolean | Executar o Step Functions após deploy |
| `start_month` | string | Mês inicial `YYYY-MM` |
| `end_month` | string | Mês final `YYYY-MM` |

---

## 4. Build e Push da Imagem Docker

### 4.1 Build local (para testes)

```bash
docker buildx build \
  --platform linux/arm64 \
  -t trip-record-data:local \
  -f app/Dockerfile \
  .
```

### 4.2 Push manual para ECR

```bash
# Autenticar no ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin <ECR_REPOSITORY_URL>

# Tag e push
docker tag trip-record-data:local <ECR_REPOSITORY_URL>:latest
docker push <ECR_REPOSITORY_URL>:latest
```

> Em operação normal, o CI/CD cuida do build e push automaticamente.

---

## 5. Execução do Pipeline Completo (Step Functions)

### 5.1 Via GitHub Actions (recomendado)

1. Ir em **Actions → Build & Deploy → Run workflow**
2. Marcar `run_pipeline: true`
3. Informar `start_month` e `end_month`
4. Clicar em **Run workflow**
5. O workflow dispara o Step Functions e aguarda conclusão (polling a cada 30s)

### 5.2 Via AWS CLI

```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"start_month": "2025-01", "end_month": "2025-12"}'
```

### 5.3 Fluxo de execução

O Step Functions executa na seguinte ordem:

```
1. DownloadTripData     (sequencial)
2. BuildRawLayer        (sequencial — depende do download)
3. BuildTrustedLayer    (sequencial — depende do raw)
4. BuildSpecializedLayer (PARALELO — 4 tasks simultâneas)
   ├── spec_hourly_volume
   ├── spec_daily_volume
   ├── spec_trip_distance
   └── spec_distance_fare
```

Cada etapa usa `runTask.sync` — o Step Functions espera a task ECS terminar antes de avançar. Se uma etapa falha, o pipeline para.

### 5.4 Monitorar execução

```bash
# Listar execuções recentes
aws stepfunctions list-executions \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --max-results 5

# Ver status de uma execução
aws stepfunctions describe-execution \
  --execution-arn <EXECUTION_ARN>
```

---

## 6. Execução de Etapas Individuais

Cada etapa pode ser executada isoladamente via ECS `run-task`:

### 6.1 Download

```bash
aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-download \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<SUBNETS>],securityGroups=[<SG_ID>],assignPublicIp=ENABLED}" \
  --overrides '{
    "containerOverrides": [{
      "name": "download-trip-data",
      "environment": [
        {"name": "START_MONTH", "value": "2025-01"},
        {"name": "END_MONTH", "value": "2025-12"}
      ]
    }]
  }'
```

### 6.2 Build Raw

```bash
aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-build-raw \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<SUBNETS>],securityGroups=[<SG_ID>],assignPublicIp=ENABLED}" \
  --overrides '{
    "containerOverrides": [{
      "name": "build-raw-layer",
      "environment": [
        {"name": "START_MONTH", "value": "2025-01"},
        {"name": "END_MONTH", "value": "2025-12"}
      ]
    }]
  }'
```

### 6.3 Build Trusted

```bash
aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-build-trusted \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<SUBNETS>],securityGroups=[<SG_ID>],assignPublicIp=ENABLED}" \
  --overrides '{
    "containerOverrides": [{
      "name": "build-trusted-layer",
      "environment": [
        {"name": "START_MONTH", "value": "2025-01"},
        {"name": "END_MONTH", "value": "2025-12"}
      ]
    }]
  }'
```

### 6.4 Build Specialized (exemplo: hourly volume)

```bash
aws ecs run-task \
  --cluster trip-record-data \
  --task-definition trip-record-data-build-spec-hourly-volume \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<SUBNETS>],securityGroups=[<SG_ID>],assignPublicIp=ENABLED}" \
  --overrides '{
    "containerOverrides": [{
      "name": "build-spec-hourly-volume",
      "environment": [
        {"name": "START_MONTH", "value": "2025-01"},
        {"name": "END_MONTH", "value": "2025-12"}
      ]
    }]
  }'
```

---

## 7. Validação e Testes

### 7.1 Validação automática (embutida no pipeline)

Cada etapa do pipeline executa validações automaticamente:

**Raw:**
- Contagem de linhas: cada tabela raw deve ter o mesmo número de linhas que o staging
- Consistência de joins: as 4 tabelas devem casar 100% por `trip_id`
- Tabelas dimensão: verifica que cada dimensão tem > 0 linhas

**Trusted:**
- Contagem de linhas (registra quantos foram filtrados e o percentual)
- Verificação de nulos em campos derivados (`company_name`, `pickup_hour`, `total_fare`, `fare_per_mile`)
- Ranges dos valores (trip_miles, fare, trip_time dentro dos limites esperados)

**Specialized:**
- Contagem de linhas > 0
- Soma de `trip_count` deve bater com o total da trusted (para tabelas com essa métrica)

Se qualquer validação crítica falha, o script retorna exit code 1 e o Step Functions marca a etapa como FAILED.

### 7.2 Validação manual via Athena

```sql
-- Verificar tabelas registradas no Glue
SELECT * FROM information_schema.tables
WHERE table_schema = 'trip_record_data';

-- Contagem por camada
SELECT 'raw_dispatch_base' AS tbl, COUNT(*) AS n FROM trip_record_data.raw_dispatch_base WHERE year_month = 202506
UNION ALL
SELECT 'trusted_trips', COUNT(*) FROM trip_record_data.trusted_trips WHERE year_month = 202506
UNION ALL
SELECT 'spec_hourly_volume', COUNT(*) FROM trip_record_data.spec_hourly_volume WHERE year_month = 202506;

-- Verificar filtros da trusted (quantos registros foram removidos)
SELECT
  (SELECT COUNT(*) FROM trip_record_data.raw_dispatch_base WHERE year_month = 202506) AS raw_count,
  (SELECT COUNT(*) FROM trip_record_data.trusted_trips WHERE year_month = 202506) AS trusted_count;
```

### 7.3 Verificar logs do ECS

```bash
# Listar log streams recentes
aws logs describe-log-streams \
  --log-group-name /ecs/trip-record-data \
  --order-by LastEventTime \
  --descending \
  --limit 5

# Filtrar por erros
aws logs filter-log-events \
  --log-group-name /ecs/trip-record-data \
  --filter-pattern "ERROR"
```

### 7.4 Verificar execução do Step Functions

```bash
# Ver histórico de uma execução
aws stepfunctions get-execution-history \
  --execution-arn <EXECUTION_ARN> \
  --query 'events[?type==`TaskFailed` || type==`TaskSucceeded`]'
```

---

## 8. Reexecução e Reprocessamento

### 8.1 Re-download de dados

O script de download é **idempotente**: arquivos que já existem no S3 são pulados. Para forçar o re-download:

```bash
aws s3 rm s3://meu-bucket/staging/fhvhv_tripdata_2025-03.parquet
```

### 8.2 Reprocessamento de uma camada específica

Cada camada pode ser reprocessada independentemente via `run-task` (seção 6). As camadas usam `OVERWRITE_OR_IGNORE` — partições existentes são sobrescritas.

### 8.3 Reprocessamento completo

Execute o Step Functions novamente com o mesmo input. Todas as camadas serão recriadas e o Glue será atualizado.

### 8.4 Adicionar novos meses

Execute o pipeline com o novo intervalo. Exemplo para adicionar julho-dezembro:

```bash
aws stepfunctions start-execution \
  --state-machine-arn <STATE_MACHINE_ARN> \
  --input '{"start_month": "2025-07", "end_month": "2025-12"}'
```

As partições novas serão criadas sem afetar as existentes.

### 8.5 Atualizar tabelas dimensão

1. Edite os CSVs em `reference/` (`dim_hvfhs_license.csv`, `dim_base.csv`)
2. Faça commit e push para `main`
3. O CI/CD rebuilda a imagem automaticamente
4. Execute o pipeline para regenerar as camadas que dependem das dimensões (raw + trusted + specialized)

---

## 9. Problemas Conhecidos e Limitações

| Problema | Descrição | Mitigação |
|----------|-----------|-----------|
| **Timeout no download** | Arquivos grandes (~500 MB) podem causar timeout em conexões instáveis | Streaming com chunks de 8 MB + multipart upload; re-executar pula arquivos já baixados |
| **Memória no build-raw/trusted** | DuckDB materializa dados em memória | Tasks com 4 GB RAM + 21 GB ephemeral; trusted usa `memory_limit='3GB'` para forçar spill to disk |
| **Dados disponíveis com atraso** | A TLC publica com ~2 meses de atraso | Verificar disponibilidade antes de configurar o intervalo |
| **Retenção de logs curta** | CloudWatch com retenção de 1 dia | Aumentar em `cloudwatch.tf` se necessário |
| **Partições não são deletadas automaticamente** | Reprocessamento cria/atualiza partições mas não remove antigas | Limpar manualmente via Glue se necessário |
| **Step Functions timeout** | Execução máxima padrão de 1 ano | Para o volume atual não é problema; monitorar se escalar |

---

## 10. Ações de Contingência

### Step Functions falhou

1. Verificar qual etapa falhou:
   ```bash
   aws stepfunctions get-execution-history \
     --execution-arn <EXECUTION_ARN> \
     --query 'events[?type==`TaskFailed`]'
   ```
2. Verificar logs da task ECS que falhou no CloudWatch
3. Corrigir o problema e re-executar o pipeline (é idempotente)

### Task ECS falhou

1. Verificar logs:
   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/trip-record-data \
     --filter-pattern "ERROR"
   ```
2. Causas comuns:
   - **Sem conectividade:** verificar `assignPublicIp=ENABLED` e security group
   - **Permissão S3/Glue:** verificar IAM role `trip-record-data-ecs-task`
   - **Out of memory:** aumentar memória da task definition
   - **Arquivo não encontrado no CDN:** mês pode não estar disponível ainda

### Terraform state corrompido

1. O bucket de state tem versionamento habilitado
2. Listar versões:
   ```bash
   aws s3api list-object-versions \
     --bucket <bucket>-tfstate \
     --prefix trip-record-data/terraform.tfstate
   ```
3. Restaurar versão anterior se necessário

### Glue tables inconsistentes

1. Re-executar o pipeline — faz upsert (create or update) das tabelas e partições
2. Para limpar manualmente:
   ```bash
   aws glue delete-table --database-name trip_record_data --name <table_name>
   ```

### Custos inesperados

1. O AWS Budgets envia alertas em 50%, 80% e 100% de $10/mês
2. Verificar AWS Cost Explorer
3. Para reduzir custos:
   - Deletar dados de staging após build-raw: `aws s3 rm s3://bucket/staging/ --recursive`
   - Reduzir o número de meses processados
   - Verificar se tasks ECS não ficaram "stuck" (running indefinidamente)
