-- Preserve the Query Engine run -> Email Service delivery -> Graph request chain.
alter table log.artifact_runs
  add column if not exists delivery_id uuid,
  add column if not exists delivery_status text,
  add column if not exists delivery_provider text,
  add column if not exists provider_message_id text,
  add column if not exists provider_status_code integer;

create index if not exists artifact_runs_delivery_id_idx
  on log.artifact_runs (delivery_id)
  where delivery_id is not null;
