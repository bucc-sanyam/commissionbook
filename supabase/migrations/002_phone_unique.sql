BEGIN;
ALTER TABLE public.clients DROP CONSTRAINT IF EXISTS clients_name_key;
DROP INDEX IF EXISTS public.idx_clients_name_ci;
CREATE INDEX IF NOT EXISTS idx_clients_name_ci ON public.clients (lower(name));
ALTER TABLE public.clients ADD CONSTRAINT clients_phone_key UNIQUE (phone);
COMMIT;
