-- Migration 010: Add deduct_from_funds flag to trades and client_shares
-- Default is 0 (false) so available cash does not go negative when trades are recorded.

ALTER TABLE public.trades 
ADD COLUMN IF NOT EXISTS deduct_from_funds INTEGER NOT NULL DEFAULT 0;

ALTER TABLE public.client_shares 
ADD COLUMN IF NOT EXISTS deduct_from_funds INTEGER NOT NULL DEFAULT 0;
