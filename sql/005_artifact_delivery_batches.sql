-- Durable, auditable delivery batches. One item represents one consolidated
-- owner notification (which may cover one or more authorized facilities).
alter table log.artifact_runs
  add column if not exists authenticated_subject text,
  add column if not exists authorized_roles jsonb not null default '[]'::jsonb,
  add column if not exists execution_mode text not null default 'live',
  add column if not exists lease_owner text,
  add column if not exists lease_expires_at timestamptz,
  add column if not exists attempt_count integer not null default 0;

create table if not exists log.artifact_delivery_batches (
  batch_id uuid primary key,
  client_key text not null,
  reporting_period text not null,
  mode text not null check (mode in ('live', 'internal_test')),
  idempotency_key text not null,
  status text not null,
  requested_by text not null,
  authorized_roles jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (client_key, idempotency_key)
);

create table if not exists app.artifact_delivery_targets (
  artifact_id uuid primary key,
  client_key text not null,
  target_key text not null,
  live_enabled boolean not null default false,
  internal_test_enabled boolean not null default false,
  batch_only boolean not null default true,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (client_key, target_key)
);

create table if not exists log.artifact_delivery_batch_items (
  item_id uuid primary key,
  batch_id uuid not null references log.artifact_delivery_batches(batch_id),
  artifact_id uuid not null,
  artifact_key text not null,
  run_id uuid not null unique,
  status text not null,
  attempt_number integer not null default 1,
  retry_reason text,
  requested_by text,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists artifact_delivery_batch_items_batch_idx
  on log.artifact_delivery_batch_items (batch_id, created_at);

create index if not exists artifact_runs_delivery_queue_idx
  on log.artifact_runs (started_at, run_id)
  where status = 'queued' and delivery_mode in ('email', 'both');
