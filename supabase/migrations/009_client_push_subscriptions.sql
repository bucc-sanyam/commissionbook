-- Migration 009: Client Web Push Notifications Subscriptions
CREATE TABLE IF NOT EXISTS public.client_push_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT REFERENCES public.clients(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_push_client ON public.client_push_subscriptions(client_id);
ALTER TABLE public.client_push_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all push actions" ON public.client_push_subscriptions FOR ALL USING (true);
