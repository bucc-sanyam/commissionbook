-- Migration 011: Add stock_price_targets table for admin price targets on charts and client portals

CREATE TABLE IF NOT EXISTS public.stock_price_targets (
    id SERIAL PRIMARY KEY,
    stock TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'T1',
    target_price NUMERIC NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_spt_stock ON public.stock_price_targets(stock);
