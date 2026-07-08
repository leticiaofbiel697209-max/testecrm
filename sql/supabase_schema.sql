create table if not exists auditoria_acoes (
  id bigserial primary key,
  usuario text,
  tipo_acao text not null,
  cliente_id text,
  cliente text,
  origem text,
  resultado text,
  observacao text,
  payload jsonb default '{}'::jsonb,
  criado_em timestamptz default now()
);

create table if not exists followup_fila (
  id bigserial primary key,
  cliente_id text,
  cliente text,
  vendedor text,
  canal text check (canal in ('email','whatsapp','telefone','outro')),
  telefone text,
  email text,
  mensagem text not null,
  status text not null default 'pendente'
    check (status in ('pendente','enviado','erro','respondido','cancelado')),
  data_programada timestamptz,
  enviado_em timestamptz,
  erro text,
  origem text,
  resposta_api jsonb default '{}'::jsonb,
  criado_em timestamptz default now()
);

create table if not exists observacoes (
  id bigserial primary key,
  cliente_id text,
  cliente text,
  vendedor text,
  observacao text,
  origem text,
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists ja_liguei (
  id bigserial primary key,
  cliente_id text,
  cliente text,
  vendedor text,
  observacao text,
  status text default 'já liguei',
  origem text,
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists retornos_programados (
  id bigserial primary key,
  cliente_id text,
  cliente text,
  vendedor text,
  data_retorno timestamptz,
  motivo text,
  observacao text,
  status text default 'pendente',
  concluido_em timestamptz,
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists historico_cliente (
  id bigserial primary key,
  cliente_id text,
  cliente text,
  vendedor text,
  tipo text,
  descricao text,
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists usuarios_vendedoras (
  id bigserial primary key,
  nome text,
  email text,
  whatsapp text,
  ativo boolean default true,
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists followup_historico (
  id bigserial primary key,
  destinatario text,
  assunto text,
  mensagem text,
  conta_saida text,
  origem text,
  enviado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists followup_prospeccoes (
  id bigserial primary key,
  cliente text,
  email text,
  vendedor text,
  status text default 'pendente',
  criado_em timestamptz default now(),
  payload jsonb default '{}'::jsonb
);

create table if not exists entregadores (
  id bigserial primary key,
  nome text,
  telefone text,
  ativo boolean default true,
  criado_em timestamptz default now()
);

create table if not exists crm_snapshots (
  chave text primary key,
  salvo_em timestamptz,
  origem text,
  payload_json jsonb,
  payload_base64 text
);

create table if not exists rotas (
  id bigserial primary key,
  data_rota date,
  entregador text,
  veiculo text,
  observacao text,
  criado_em timestamptz default now()
);

create table if not exists entregas (
  id bigserial primary key,
  rota_id bigint references rotas(id),
  numero_venda text,
  cliente text,
  telefone text,
  endereco text,
  cidade text,
  status text default 'PENDENTE',
  observacao text,
  origem_pedido text,
  loja_id text,
  data_entrega timestamptz,
  atualizado_por text,
  criado_em timestamptz default now()
);

create table if not exists ocorrencias (
  id bigserial primary key,
  entrega_id bigint references entregas(id),
  tipo text,
  descricao text,
  criado_em timestamptz default now()
);

create index if not exists idx_followup_fila_status_data on followup_fila(status, data_programada);
create index if not exists idx_auditoria_cliente on auditoria_acoes(cliente_id, criado_em desc);
