ALTER TABLE public.client_alert_views ADD COLUMN IF NOT EXISTS acknowledged INTEGER NOT NULL DEFAULT 0;
ALTER TABLE public.client_alert_views ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
