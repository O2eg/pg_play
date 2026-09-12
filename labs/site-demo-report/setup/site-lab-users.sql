\set ON_ERROR_STOP on
-- Roles: owner and group hierarchy ---------------------------------------
create role lab_owner nologin;
comment on role lab_owner is 'Owner of the lab schema';
create role lab_readonly nologin;
comment on role lab_readonly is 'Read-only access group';
create role lab_readwrite nologin;
comment on role lab_readwrite is 'Read-write access group';
create role lab_admin nologin createdb;
comment on role lab_admin is 'Lab administrators';
create role lab_bypass nologin bypassrls;
create role lab_empty_group nologin;
comment on role lab_empty_group is 'Group without members (cleanup candidate)';
grant lab_readonly to lab_readwrite;
grant lab_readwrite to lab_admin with admin option;
-- Login users -------------------------------------------------------------
create role lab_analyst login password 'lab-analyst-pw' valid until '2027-06-30';
create role lab_app login password 'lab-app-pw' connection limit 20;
create role lab_etl login password 'lab-etl-pw';
create role lab_expired login password 'lab-expired-pw' valid until '2024-01-01';
create role lab_dba login password 'lab-dba-pw' createrole;
set password_encryption = 'md5';
create role lab_legacy login password 'lab-legacy-pw';
reset password_encryption;
grant lab_readonly to lab_analyst;
grant lab_readwrite to lab_app;
grant lab_readwrite to lab_etl;
grant lab_bypass to lab_etl;
grant lab_admin to lab_dba;
grant lab_readonly to lab_expired;
-- Per-role and per-database settings -------------------------------------
alter role lab_analyst set statement_timeout = '30s';
alter role lab_analyst set work_mem = '32MB';
alter role lab_app in database workload_trace set search_path = 'lab, public';
alter role lab_etl set lock_timeout = '5s';
alter database workload_trace set log_min_duration_statement = '500ms';
-- Database-level privileges ---------------------------------------------
revoke connect on database workload_trace from public;
grant connect on database workload_trace to lab_readonly;
grant connect, create on database workload_trace to lab_admin;
grant temporary on database workload_trace to lab_readwrite;
-- Schema and objects ------------------------------------------------------
create schema lab authorization lab_owner;
grant usage on schema lab to lab_readonly;
grant usage, create on schema lab to lab_admin;
set role lab_owner;
create table lab.customers (
  id bigserial primary key,
  name text not null,
  email text not null,
  region text not null,
  created_at timestamptz not null default now()
);
create table lab.orders (
  id bigserial primary key,
  customer_id bigint not null references lab.customers (id),
  amount numeric(12, 2) not null,
  status text not null default 'new',
  created_at timestamptz not null default now()
);
create index on lab.orders (customer_id);
create sequence lab.invoice_seq;
create view lab.customer_summary as
  select c.id, c.name, c.region, count(o.id) as orders, coalesce(sum(o.amount), 0) as total
  from lab.customers c left join lab.orders o on o.customer_id = c.id
  group by c.id, c.name, c.region;
create materialized view lab.region_totals as
  select region, count(*) as customers from lab.customers group by region;
create domain lab.money_amount as numeric(12, 2) check (value >= 0);
create function lab.order_total(p_customer bigint) returns numeric
  language sql stable security definer
  as 'select coalesce(sum(amount), 0) from lab.orders where customer_id = p_customer';
create function lab.next_invoice() returns bigint
  language sql volatile
  as 'select nextval(''lab.invoice_seq'')';
insert into lab.customers (name, email, region)
  select 'customer ' || g, 'user' || g || '@example.com', (array['north','south','east','west'])[1 + g % 4]
  from generate_series(1, 2000) g;
insert into lab.orders (customer_id, amount, status)
  select 1 + (g % 2000), (g % 500) + 0.5, (array['new','paid','shipped'])[1 + g % 3]
  from generate_series(1, 20000) g;
refresh materialized view lab.region_totals;
-- Object privileges: groups vs users, PUBLIC, grant option, column, RLS ---
grant select on all tables in schema lab to lab_readonly;
grant select, insert, update, delete on lab.customers, lab.orders to lab_readwrite;
grant usage on all sequences in schema lab to lab_readwrite;
grant execute on function lab.order_total(bigint) to lab_readonly;
revoke execute on function lab.next_invoice() from public;
grant execute on function lab.next_invoice() to lab_readwrite;
grant usage on domain lab.money_amount to lab_readonly;
grant select on lab.customer_summary to public;
grant select on lab.orders to lab_analyst with grant option;
grant select (id, name, region) on lab.customers to lab_app;
grant update (status) on lab.orders to lab_app;
alter table lab.customers enable row level security;
create policy customers_region_ro on lab.customers for select to lab_readonly using (region <> 'west');
create policy customers_all_rw on lab.customers to lab_readwrite using (true) with check (true);
alter default privileges for role lab_owner in schema lab grant select on tables to lab_readonly;
alter default privileges for role lab_owner in schema lab grant select, insert, update, delete on tables to lab_readwrite;
alter default privileges for role lab_owner in schema lab grant usage on sequences to lab_readwrite;
reset role;
-- Large objects, publication, tablespace ---------------------------------
do $$
declare o oid;
begin
  for i in 1..5 loop
    o := lo_create(0);
    execute format('grant select on large object %s to lab_readonly', o);
    execute format('grant update on large object %s to lab_readwrite', o);
  end loop;
end $$;
create publication lab_orders_pub for table lab.orders;
grant create on tablespace pg_default to lab_admin;
-- Privileges on workload profile schemas ---------------------------------
do $$
declare s record;
begin
  for s in
    select nspname from pg_namespace
    where nspname not in ('pg_catalog', 'information_schema', 'lab', 'public')
      and nspname not like 'pg_%'
  loop
    execute format('grant usage on schema %I to lab_readonly, lab_readwrite', s.nspname);
    execute format('grant select on all tables in schema %I to lab_readonly', s.nspname);
    execute format('grant select, insert, update, delete on all tables in schema %I to lab_readwrite', s.nspname);
    execute format('grant usage on all sequences in schema %I to lab_readwrite', s.nspname);
  end loop;
end $$;
select count(*) as roles from pg_roles where rolname like 'lab\_%';

-- 2026-09-12: a real ACL drift on an extension object for object_workload.extension_objects_acl_drift
-- (pg_init_privs baseline vs current ACL); lab_analyst can read query statistics.
grant select on public.pg_stat_statements to lab_analyst;
