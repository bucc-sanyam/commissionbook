"""Synthetic cases and a supplied browser-Tesseract transcript from a synthetic PNG.

These do not establish coverage of real broker screenshots.
"""

import copy
import itertools
import textwrap
import unittest

from trade_parser import (
    detect_platform,
    extract_orders,
    find_company_name,
    find_price,
    find_qty,
    find_symbol,
    pair_orders,
    parse_date,
    parse_text,
)

BROWSER_TESSERACT_DETAIL_TEXT = (
    "Groww\nTATAMOTORS\n\nBuy\n\nCompleted\n\nQuantity 10\n\n"
    "Avg price INR 950.25\n12 Sep 2026\n"
)


def parse(text, default_year=None):
    return parse_text(textwrap.dedent(text).strip(), default_year)


def order(side, qty, day, price=100, symbol="INFY", **extra):
    return {"symbol": symbol, "side": side, "qty": qty, "price": price, "date": day, **extra}


class DateTests(unittest.TestCase):
    def test_explicit_dates(self):
        for text in (
            "2024-09-12", "12/09/2024", "12-09-24", "12.09.2024",
            "12 Sep 2024", "12 Sep, 2024", "12 Sep,2024",
            "12th September '24", "September 12, 2024",
            "Date:12/09/2024", "Executed 12 Sep 2024 10:15:02",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_date(text), "2024-09-12")

    def test_missing_year_is_not_current_year(self):
        for text in ("12 Sep", "September 12", "Date: 12/09", "12/09"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))
                self.assertEqual(parse_date(text, 2024), "2024-09-12")

    def test_times_are_not_years_or_days(self):
        for text in (
            "12 Sep 10:15:02", "Sep 12, 10:15:02", "12 Sep 10:15 PM",
            "09:12 May 2024", "Sep 10:15:02", "10:15:02",
            "12 Sep\n24", "12 Sep 24.50", "12 Sep 10/10",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))

    def test_share_counts_after_partial_dates_are_not_years(self):
        self.assertIsNone(parse_date("12 Sep 10 shares"))
        self.assertEqual(find_qty("12 Sep 10 shares"), 10)
        self.assertEqual(parse_date("12 Sep 2024 Qty4"), "2024-09-12")

    def test_month_prefixes_are_not_dates(self):
        for text in ("12 Market 2024", "12 Septemberish 2024", "12 Maybe 2024", "Sepia 12 2024"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))

    def test_invalid_dates_remain_unknown(self):
        for text in (
            "31 Sep 2024", "2024-13-01", "29/02/2023", "12/09/1999",
            "12/09/999999", "12 Sep 20249", "00/09/2024", "32/09/2024",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text))
        self.assertEqual(parse_date("29/02/2024"), "2024-02-29")

    def test_default_year_cannot_hide_a_malformed_explicit_year(self):
        for text in ("12 Sep 20249", "12 Sep 999", "12 Sep 1999"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text, 2024))
        for year in (0, 24, True, 2024.5, "unknown"):
            with self.subTest(year=year):
                self.assertIsNone(parse_date("12 Sep", year))

    def test_conflicting_dates_are_not_silently_selected(self):
        self.assertIsNone(parse_date("12 Sep 2024 to 25 Sep 2024"))
        self.assertEqual(parse_date("12 Sep 2024 / 12/09/2024"), "2024-09-12")

    def test_relative_dates_are_not_resolved_against_today(self):
        for text in ("Today", "Yesterday 10:15:02", "Executed today"):
            with self.subTest(text=text):
                self.assertIsNone(parse_date(text, 2024))

    def test_explicit_default_year_is_disclosed(self):
        result = parse("""
            INFY
            BUY Qty 2
            Avg 1500
            Executed 12 Sep 10:15:02
        """, 2024)
        self.assertEqual(result["orders"][0]["date"], "2024-09-12")
        self.assertTrue(any("default_year" in warning for warning in result["warnings"]))

    def test_missing_year_has_warning_and_blank_trade_date(self):
        result = parse("""
            INFY
            BUY Qty 2
            Avg 1500
            Executed 12 Sep 10:15:02
        """)
        self.assertIsNone(result["orders"][0]["date"])
        self.assertEqual(result["trades"][0]["buy_date"], "")
        self.assertTrue(any("no year" in warning for warning in result["warnings"]))

    def test_two_digit_year_assumption_is_disclosed(self):
        result = parse("INFY\nBUY Qty 2\nAvg 1500\nExecuted 12/09/24")
        self.assertEqual(result["orders"][0]["date"], "2024-09-12")
        self.assertTrue(any("two-digit year" in warning for warning in result["warnings"]))

    def test_default_year_does_not_turn_filled_total_into_date(self):
        result = parse("BUY 10/10 COMPLETE\nINFY NSE\nAvg 1500\nCNC MARKET 10:15:02", 2024)
        self.assertIsNone(result["orders"][0]["date"])
        self.assertEqual(result["orders"][0]["qty"], 10)

    def test_execution_date_is_not_placement_date(self):
        result = parse("""
            INFY
            BUY Qty 2
            Avg 1500
            Order placed 11 Sep 2024 16:00:00
            Executed 12 Sep 2024 09:15:02
        """)
        self.assertEqual(result["orders"][0]["date"], "2024-09-12")

    def test_placement_date_alone_does_not_establish_execution_date(self):
        result = parse("INFY\nBUY Qty 2\nAvg 1500\nCOMPLETE\nOrder date: 11 Sep 2024")
        self.assertIsNone(result["orders"][0]["date"])


class DetailDateRegressionTests(unittest.TestCase):
    def test_exact_browser_tesseract_transcript(self):
        result = parse_text(BROWSER_TESSERACT_DETAIL_TEXT)
        self.assertEqual(result["orders"], [order("buy", 10, "2026-09-12", 950.25, "TATAMOTORS")])
        self.assertEqual(result["trades"], [{
            "stock": "TATAMOTORS", "quantity": 10, "buy_price": 950.25,
            "sell_price": "", "buy_date": "2026-09-12", "sell_date": "",
        }])
        self.assertEqual(result["warnings"], [])

    def test_standalone_status_does_not_hide_supported_date_formats(self):
        for status in ("Completed", "Executed", "Filled", "COMPLETE"):
            for date_text in (
                "12 Sep 2026", "12 September 2026", "September 12, 2026",
                "2026-09-12", "12/09/2026", "12-09-2026", "12.09.2026",
                "12 Sep 2026 10:15:02",
            ):
                with self.subTest(status=status, date_text=date_text):
                    text = BROWSER_TESSERACT_DETAIL_TEXT.replace("Completed", status).replace("12 Sep 2026", date_text)
                    self.assertEqual(parse_text(text)["trades"][0]["buy_date"], "2026-09-12")

    def test_long_detail_card_date_survives_intervening_fields(self):
        result = parse("""
            Groww
            TATAMOTORS
            Buy
            Completed
            Order type
            Market
            Product type
            CNC
            Exchange
            NSE
            Quantity
            10
            Avg price
            INR 950.25
            Order placed
            11 Sep 2026 09:30:00
            12 Sep 2026
        """)
        self.assertEqual(result["orders"], [order("buy", 10, "2026-09-12", 950.25, "TATAMOTORS")])

    def test_execution_time_label_does_not_suppress_full_date_elsewhere_in_card(self):
        text = BROWSER_TESSERACT_DETAIL_TEXT.replace(
            "Completed\n\n", "Completed\n\nExecution time\n10:15:02\n\n")
        self.assertEqual(parse_text(text)["trades"][0]["buy_date"], "2026-09-12")

    def test_detail_cards_do_not_borrow_previous_or_next_date(self):
        dated = BROWSER_TESSERACT_DETAIL_TEXT.replace("Groww\n", "", 1)
        undated = "INFY\nBuy\nCompleted\nQuantity 2\nAvg price INR 1500\n"
        for text, expected in (
            (undated + dated, [("INFY", None), ("TATAMOTORS", "2026-09-12")]),
            (dated + undated, [("TATAMOTORS", "2026-09-12"), ("INFY", None)]),
        ):
            with self.subTest(expected=expected):
                result = parse_text(text)
                self.assertEqual([(o["symbol"], o["date"]) for o in result["orders"]], expected)

    def test_empty_execution_date_field_does_not_cross_card_boundary(self):
        result = parse("""
            TATAMOTORS
            Buy
            Completed
            Quantity 10
            Avg price INR 950.25
            Execution date:
            INFY
            Buy Qty2
            Avg1500
            Executed 13 Sep 2026
        """)
        self.assertEqual([o["date"] for o in result["orders"]], [None, "2026-09-13"])

    def test_explicit_execution_date_keeps_precedence_over_other_card_dates(self):
        result = parse("""
            TATAMOTORS
            Buy
            Completed
            Quantity 10
            Avg price INR 950.25
            11 Sep 2026
            Executed on
            12 Sep 2026
        """)
        self.assertEqual(result["trades"][0]["buy_date"], "2026-09-12")

    def test_placement_only_date_is_not_promoted_by_standalone_status(self):
        for placed in ("Order date: 11 Sep 2026", "Order placed\n11 Sep 2026"):
            with self.subTest(placed=placed):
                result = parse_text("INFY\nBuy Qty2\nAvg1500\nCompleted\n" + placed)
                self.assertIsNone(result["orders"][0]["date"])

    def test_explicit_incomplete_execution_date_does_not_use_placement_year(self):
        result = parse("""
            INFY
            Buy Qty2
            Completed
            Avg1500
            Order date: 11 Sep 2026
            Executed on
            12 Sep
        """)
        self.assertIsNone(result["orders"][0]["date"])
        self.assertTrue(any("no year" in warning for warning in result["warnings"]))


class FieldTests(unittest.TestCase):
    def test_quantity_labels_and_counts(self):
        for text, expected in (
            ("Qty4", 4), ("QTY: 4", 4), ("Quantity\n4", 4),
            ("Qty: 1,000", 1000), ("Qty 1,23,456", 123456),
            ("Buy \u00b7 5 shares", 5), ("Shares: 5", 5),
            ("BUY 10/10 COMPLETE", 10), ("SELL 3 / 10 COMPLETE", 3),
            ("10/10 COMPLETE", 10), ("BUY\n10/10 COMPLETE", 10),
            ("Qty 3/10", 3), ("BUY 5 @ 1500", 5),
        ):
            with self.subTest(text=text):
                self.assertEqual(find_qty(text), expected)

    def test_filled_quantity_takes_precedence_over_requested_quantity(self):
        self.assertEqual(find_qty("Qty 10\nFilled qty: 3"), 3)
        self.assertEqual(find_qty("Quantity 10\nFilled: 3"), 3)
        self.assertEqual(find_qty("Qty 10\nExecuted quantity 3"), 3)

    def test_price_before_quantity_label_is_not_a_quantity(self):
        self.assertEqual(find_qty("Buy avg 1650.25 Qty4 13/09/2024"), 4)
        self.assertEqual(find_qty("Buy avg 1650 Qty 4 13/09/2024"), 4)

    def test_partially_filled_status_does_not_use_requested_quantity(self):
        self.assertIsNone(find_qty("Partially filled\nQty 10"))
        self.assertEqual(find_qty("Partially filled\nQty10\nFilled qty3"), 3)

    def test_dates_are_never_quantities(self):
        for text in (
            "13/09/2024", "12-09-24", "2024/09/12", "12 Sep 2024",
            "Date:10/10", "10/10", "10:15:02", "Executed 12/09/2024",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_qty(text))

    def test_bad_quantities_are_not_truncated_or_invented(self):
        for text in (
            "Qty -5", "-5 shares", "Qty 2.5", "Qty 0", "Qty 10000000",
            "Qty 10/5", "BUY 10/0 COMPLETE", "BUY 0/10 COMPLETE",
            "Qty 4\nQty 5", "Qty 12,34,56",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_qty(text))

    def test_executed_prices_win_over_current_prices(self):
        for text in (
            "Avg.1,500.00 LTP1,510.40",
            "LTP \u20b91,510.40 Avg \u20b91,500.00",
            "Current price: 1510.40\nAverage price\n\u20b91,500.00",
            "Limit price: 1510.40\nExecuted price: 1500.00",
            "Trigger price 1490\nFilled price 1500",
        ):
            with self.subTest(text=text):
                self.assertEqual(find_price(text), 1500)

    def test_no_ltp_or_decimal_fallback(self):
        for text in (
            "LTP 1,510.40", "LTP \u20b91,510.40", "LTP\n\u20b91,510.40",
            "LTP: Avg \u20b91,510.40", "L T P\n\u20b91,510.40",
            "Current price: \u20b91,510.40", "Current holding price \u20b91,510.40",
            "Current market price\n\u20b91,510.40", "Latest traded price\n\u20b91,510.40",
            "Current average price \u20b91,510.40", "Current holding avg price \u20b91,510.40",
            "Holdings\n\u20b91,510.40", "Market price 1,510.40",
            "Limit price \u20b91,510.40", "Trigger price \u20b91,510.40",
            "Invested amount \u20b912,000", "Total \u20b912,000",
            "Order value \u20b912,000", "Charges \u20b915.40",
            "Price \u20b91,510.40",
            "1,510.40", "%1500", "z1500", "12.09.2024", "10:15:02",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_price(text))

    def test_prices_must_be_positive_unambiguous_numbers(self):
        for text in ("Avg -5", "Avg 0", "Avg 100\nAvg 200", "Avg 12,34,56"):
            with self.subTest(text=text):
                self.assertIsNone(find_price(text))
        self.assertEqual(find_price("Avg INR 1,23,456.75"), 123456.75)
        self.assertEqual(find_price("Rs. 1,500.00"), 1500)

    def test_symbols_require_a_structural_position(self):
        for text, expected in (
            ("INFY", "INFY"), ("INFY NSE", "INFY"), ("NSE:INFY", "INFY"),
            ("INFY:NSE", "INFY"), ("NSETECH", "NSETECH"), ("SYMBOLINFY", "SYMBOLINFY"),
            ("Symbol: RELIANCE", "RELIANCE"), ("Trading symbol: INFY", "INFY"),
            ("M&M", "M&M"), ("BAJAJ-AUTO BSE", "BAJAJ-AUTO"),
            ("BUY INFY Qty 2", "INFY"),
        ):
            with self.subTest(text=text):
                self.assertEqual(find_symbol(text), expected)
        self.assertIsNone(find_symbol("Your INFY order details"))

    def test_ui_labels_are_not_stock_names(self):
        for text in (
            "PORTFOLIO", "Funds", "Available Margin", "Order Book", "Order Details",
            "Current Value", "Average Price", "Execution Date", "Trade History",
            "TOTAL", "Today", "BUY SELL", "GROWW", "KITE", "NSE", "Shares",
            "LTD", "Limited", "Tradebook", "Orderbook", "Investments", "Home", "And", "The",
        ):
            with self.subTest(text=text):
                self.assertIsNone(find_symbol(text))
                self.assertIsNone(find_company_name(text))

    def test_company_names_are_not_truncated(self):
        for name in ("Reliance Industries", "Reliance Industries Ltd", "Tata Motors", "Bank of Baroda"):
            with self.subTest(name=name):
                self.assertEqual(find_company_name(name), name)

    def test_unknown_brand_stays_generic(self):
        self.assertEqual(detect_platform("Groww orders"), "groww")
        self.assertEqual(detect_platform("Zerodha Console tradebook"), "zerodha")
        self.assertEqual(detect_platform("Kite order details"), "zerodha")
        self.assertEqual(detect_platform("unconsoleable"), "generic")
        self.assertEqual(detect_platform("BUY 10/10 COMPLETE\nINFY NSE"), "generic")


class LayoutTests(unittest.TestCase):
    def test_groww_completed_cards(self):
        result = parse("""
            Groww
            Reliance Industries
            Buy \u00b7 5 shares
            \u20b92,450.50
            Executed \u00b7 12 Sep 2024
            Reliance Industries
            Sell \u00b7 5 shares
            \u20b92,550.75
            Executed \u00b7 25 Sep 2024
        """)
        self.assertEqual(result["platform"], "groww")
        self.assertEqual(result["orders"], [
            order("buy", 5, "2024-09-12", 2450.50, "Reliance Industries"),
            order("sell", 5, "2024-09-25", 2550.75, "Reliance Industries"),
        ])
        self.assertEqual(result["trades"], [{
            "stock": "Reliance Industries", "quantity": 5,
            "buy_price": 2450.50, "sell_price": 2550.75,
            "buy_date": "2024-09-12", "sell_date": "2024-09-25",
        }])
        self.assertEqual(result["warnings"], [])

    def test_groww_detail_symbol_and_multiline_labels(self):
        result = parse("""
            Groww
            Reliance Industries
            RELIANCE NSE
            Order details
            Buy
            Quantity
            5
            Average price
            \u20b92,450.50
            Order placed
            11 Sep 2024 16:00:00
            Executed on
            12 Sep 2024 09:15:02
        """)
        self.assertEqual(result["orders"], [order("buy", 5, "2024-09-12", 2450.50, "RELIANCE")])

    def test_repeated_side_on_price_field_is_not_another_order(self):
        result = parse("""
            INFY
            BUY Qty2
            Buy average 1500
            Executed 12 Sep 2024
        """)
        self.assertEqual(result["orders"], [order("buy", 2, "2024-09-12", 1500)])

    def test_kite_card_without_date(self):
        result = parse("""
            BUY 10/10 COMPLETE
            INFY NSE
            Avg.1,500.00 LTP1,510.40
            CNC MARKET 10:15:02
        """)
        self.assertEqual(result["orders"], [order("buy", 10, None, 1500)])
        self.assertEqual(result["trades"][0]["buy_date"], "")
        self.assertTrue(any("date" in warning for warning in result["warnings"]))

    def test_console_tradebook_rows_are_chronological(self):
        result = parse("""
            Console
            Symbol Trade date Exchange Segment Trade type Quantity Price
            INFY 2024-09-25 NSE EQ sell 4 1,700.00
            INFY 2024-09-13 NSE EQ buy 4 1,650.25
        """)
        self.assertEqual(result["platform"], "zerodha")
        self.assertEqual(result["trades"], [{
            "stock": "INFY", "quantity": 4, "buy_price": 1650.25,
            "sell_price": 1700, "buy_date": "2024-09-13", "sell_date": "2024-09-25",
        }])

    def test_console_row_ids_do_not_become_prices(self):
        result = parse("INFY | 12/09/2024 | NSE EQ | BUY | 10 | 1500.00 | 987654321 | 10:15:02")
        self.assertEqual(result["orders"], [order("buy", 10, "2024-09-12", 1500)])

    def test_pnl_shared_quantity_and_side_specific_dates(self):
        result = parse("""
            HDFCBANK
            Qty4
            Buy avg \u20b91,650.25 13/09/2024
            Sell avg \u20b91,700 25/09/2024
        """)
        self.assertEqual(result["orders"], [
            order("buy", 4, "2024-09-13", 1650.25, "HDFCBANK"),
            order("sell", 4, "2024-09-25", 1700, "HDFCBANK"),
        ])
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["quantity"], 4)

    def test_pnl_does_not_borrow_quantity_from_other_side(self):
        result = parse("""
            HDFCBANK
            Buy avg \u20b91,650.25 Qty4 13/09/2024
            Sell avg \u20b91,700 25/09/2024
        """)
        self.assertEqual([o["qty"] for o in result["orders"]], [4, None])
        self.assertEqual(len(result["trades"]), 2)
        self.assertTrue(any(t["quantity"] == "" for t in result["trades"]))

    def test_undated_pnl_legs_are_not_closed_trades(self):
        result = parse("HDFCBANK\nQty4\nBuy avg 1650.25\nSell avg 1700")
        self.assertEqual(len(result["trades"]), 2)
        self.assertFalse(any(t["buy_price"] and t["sell_price"] for t in result["trades"]))

    def test_explicit_partial_fill_uses_filled_not_total(self):
        result = parse("BUY 3/10 COMPLETE\nINFY NSE\nAvg 1500\nExecuted 12 Sep 2024")
        self.assertEqual(result["orders"][0]["qty"], 3)

    def test_zero_fill_is_not_an_execution(self):
        for quantity in ("BUY 0/10 COMPLETE", "BUY\nQty 10\nFilled: 0"):
            with self.subTest(quantity=quantity):
                result = parse_text(quantity + "\nINFY NSE\nAvg 1500\n12 Sep 2024")
                self.assertEqual(result["orders"], [])
                self.assertEqual(result["trades"], [])
                self.assertTrue(result["warnings"])

    def test_nonexecuted_statuses_are_rejected(self):
        for status in (
            "CANCELLED", "CANCELED", "REJECTED", "PENDING", "OPEN", "UNEXECUTED",
            "UNFILLED", "NOT EXECUTED", "NOT FILLED", "EXPIRED", "FAILED",
            "AWAITING", "QUEUED", "PLACED", "SUBMITTED", "REQUESTED", "AMO",
        ):
            with self.subTest(status=status):
                result = parse_text(f"INFY\nBUY Qty 5\nAvg 1500\n{status}\n12 Sep 2024")
                self.assertEqual(result["orders"], [])
                self.assertEqual(result["trades"], [])
                self.assertTrue(result["warnings"])

    def test_negative_status_overrides_complete(self):
        result = parse("INFY\nBUY Qty 5\nAvg 1500\nCOMPLETE\nCancelled\n12 Sep 2024")
        self.assertEqual(result["orders"], [])

    def test_rejected_table_rows_cannot_bypass_status_filter(self):
        for status in ("REJECTED", "PENDING", "NOT EXECUTED"):
            with self.subTest(status=status):
                result = parse_text(f"INFY 2024-09-12 NSE EQ BUY 5 1500 {status}")
                self.assertEqual(result["orders"], [])

    def test_table_like_ltp_is_not_an_execution_price(self):
        result = parse("INFY 2024-09-12 NSE EQ BUY 5 1500 LTP")
        self.assertFalse(any(o["price"] is not None for o in result["orders"]))

    def test_global_status_tab_counts_do_not_reject_executed_cards(self):
        result = parse("""
            Orders
            Open (0)
            Pending 0
            Executed (1)
            INFY
            Buy Qty2
            Avg 1500
            Executed 12 Sep 2024
        """)
        self.assertEqual(result["orders"], [order("buy", 2, "2024-09-12", 1500)])

    def test_status_with_a_count_inside_card_is_not_a_global_tab(self):
        result = parse("INFY\nBuy Qty5\nAvg1500\nPending (1)\n12 Sep 2024")
        self.assertEqual(result["orders"], [])
        self.assertTrue(any("pending" in warning for warning in result["warnings"]))

    def test_unlabelled_currency_requires_execution_evidence(self):
        result = parse("INFY\nBUY Qty 2\n\u20b91,500\n12 Sep 2024")
        self.assertIsNone(result["orders"][0]["price"])
        self.assertTrue(any("status" in warning for warning in result["warnings"]))

    def test_complete_limit_order_does_not_turn_request_price_into_fill_price(self):
        for price_line in ("Price 1500", "\u20b91,500", "@1500"):
            with self.subTest(price_line=price_line):
                result = parse_text(
                    "INFY\nBUY Qty2\nOrder type LIMIT\nCOMPLETE\n"
                    + price_line + "\nExecuted 12 Sep 2024")
                self.assertIsNone(result["orders"][0]["price"])
        result = parse("INFY\nBUY Qty2\nOrder type LIMIT\nPrice1500\nAvg1490\nExecuted 12 Sep 2024")
        self.assertEqual(result["orders"][0]["price"], 1490)

    def test_empty_and_ui_only_text_does_not_invent_orders(self):
        for text in ("", "Orders\nBuy\nSell\nHoldings\nPortfolio", "Buy Sell\nAverage Price\nNSE"):
            with self.subTest(text=text):
                result = parse_text(text)
                self.assertEqual(result["orders"], [])
                self.assertEqual(result["trades"], [])
                self.assertTrue(result["warnings"])

    def test_result_shape_and_warning_contract(self):
        result = parse("BUY Qty2\nAvg 1500\nExecuted 12 Sep")
        self.assertEqual(set(result), {"platform", "orders", "trades", "warnings"})
        self.assertEqual(set(result["orders"][0]), {"symbol", "side", "qty", "price", "date"})
        self.assertEqual(set(result["trades"][0]), {
            "stock", "quantity", "buy_price", "sell_price", "buy_date", "sell_date",
        })
        self.assertTrue(all(isinstance(warning, str) for warning in result["warnings"]))
        self.assertEqual(len(result["warnings"]), len(set(result["warnings"])))
        self.assertEqual(extract_orders("BUY Qty2\nAvg 1500\nExecuted 12 Sep"), result["orders"])


class CardBoundaryTests(unittest.TestCase):
    def test_missing_fields_do_not_come_from_previous_or_next_card(self):
        result = parse("""
            RELIANCE
            Buy Qty 5
            Avg 2450
            Executed 12 Sep 2024
            TCS
            Sell Qty 2
            LTP \u20b94000
            INFY
            Buy Qty 3
            Avg 1500
            Executed 25 Sep 2024
        """)
        self.assertEqual(result["orders"], [
            order("buy", 5, "2024-09-12", 2450, "RELIANCE"),
            order("sell", 2, None, None, "TCS"),
            order("buy", 3, "2024-09-25", 1500),
        ])

    def test_missing_symbol_is_not_previous_symbol(self):
        result = parse("""
            INFY
            Buy Qty2
            Avg 1500
            Executed 12 Sep 2024
            Sell Qty2
            Avg 1600
            Executed 25 Sep 2024
        """)
        self.assertEqual([o["symbol"] for o in result["orders"]], ["INFY", ""])
        self.assertEqual(len(result["trades"]), 2)

    def test_missing_symbol_is_not_next_symbol(self):
        result = parse("""
            Buy Qty2
            Avg 1500
            Executed 12 Sep 2024
            TCS
            Sell Qty3
            Avg 4000
            Executed 25 Sep 2024
        """)
        self.assertEqual([o["symbol"] for o in result["orders"]], ["", "TCS"])

    def test_immediately_following_header_belongs_to_next_side(self):
        for gap in ("\n", "\n\n"):
            with self.subTest(gap=gap):
                result = parse("Buy Qty2\nTCS" + gap + "Sell Qty3\nAvg 4000\nExecuted 25 Sep 2024")
                self.assertEqual(result["orders"], [
                    order("buy", 2, None, None, ""),
                    order("sell", 3, "2024-09-25", 4000, "TCS"),
                ])

    def test_missing_quantity_stays_unknown(self):
        result = parse("""
            INFY
            Buy Qty10
            Avg 1500
            Executed 12 Sep 2024
            TCS
            Buy
            Avg 4000
            Executed 25 Sep 2024
        """)
        self.assertEqual([o["qty"] for o in result["orders"]], [10, None])

    def test_status_is_scoped_to_its_card(self):
        result = parse("""
            INFY
            Buy Qty10
            Avg 1500
            Pending
            12 Sep 2024
            TCS
            Buy Qty2
            Avg 4000
            Executed 25 Sep 2024
        """)
        self.assertEqual(result["orders"], [order("buy", 2, "2024-09-25", 4000, "TCS")])

    def test_kite_cards_have_independent_prices_and_dates(self):
        result = parse("""
            Kite
            BUY 10/10 COMPLETE
            INFY NSE
            LTP \u20b91,510.40
            CNC MARKET 10:15:02
            SELL 2/2 COMPLETE
            TCS NSE
            Avg \u20b94,000 LTP \u20b94,010
            Executed 25 Sep 2024 11:15:02
        """)
        self.assertEqual(result["orders"], [
            order("buy", 10, None, None),
            order("sell", 2, "2024-09-25", 4000, "TCS"),
        ])

    def test_screenshot_separator_does_not_share_pnl_header(self):
        for separator in ("\f", "\n---\n", "\nScreenshot 2\n", "\n\n", "\nKite\n"):
            with self.subTest(separator=separator):
                result = parse_text(
                    "INFY\nQty4\nBuy avg1500 12 Sep 2024" + separator
                    + "Sell avg1600 25 Sep 2024")
                self.assertEqual([o["symbol"] for o in result["orders"]], ["INFY", ""])
                self.assertEqual([o["qty"] for o in result["orders"]], [4, None])
                self.assertEqual(len(result["trades"]), 2)

    def test_ordinary_card_fields_may_have_ocr_paragraph_gaps(self):
        result = parse("Groww\n\nReliance Industries\n\nBuy 5 shares\n\n\u20b92,450.50\n\nExecuted 12 Sep 2024")
        self.assertEqual(result["orders"], [order("buy", 5, "2024-09-12", 2450.50, "Reliance Industries")])
        result = parse("INFY\nBUY Qty2\nBuy average 1500\n\nExecuted 12 Sep 2024")
        self.assertEqual(result["orders"], [order("buy", 2, "2024-09-12", 1500)])

    def test_known_fields_can_match_across_screenshot_boundaries(self):
        result = parse("""
            Groww
            INFY
            Buy Qty4
            Avg1500
            Executed 12 Sep 2024
            Screenshot 2
            Kite
            SELL 4/4 COMPLETE
            INFY NSE
            Avg1600
            Executed 25 Sep 2024
        """)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["quantity"], 4)

    def test_incomplete_pnl_does_not_borrow_other_leg_date(self):
        result = parse("INFY\nQty4\nBuy avg1500\nSell avg1600 25 Sep 2024")
        self.assertEqual([o["date"] for o in result["orders"]], [None, "2024-09-25"])
        self.assertEqual(len(result["trades"]), 2)


class FifoTests(unittest.TestCase):
    def test_fifo_is_chronological_with_partial_fills(self):
        trades = pair_orders([
            order("sell", 12, "2024-09-03", 120),
            order("buy", 5, "2024-09-02", 110),
            order("buy", 10, "2024-09-01", 100),
        ])
        self.assertEqual([(t["quantity"], t["buy_price"], t["sell_price"]) for t in trades], [
            (10, 100, 120), (2, 110, 120), (3, 110, ""),
        ])

    def test_distinct_date_fifo_is_independent_of_ocr_order(self):
        orders = [
            order("buy", 10, "2024-09-01", 100),
            order("buy", 5, "2024-09-02", 110),
            order("sell", 12, "2024-09-03", 120),
        ]
        expected = pair_orders(orders)
        for shuffled in itertools.permutations(orders):
            with self.subTest(shuffled=shuffled):
                self.assertEqual(pair_orders(shuffled), expected)

    def test_fifo_conserves_both_sides_quantities(self):
        orders = [
            order("sell", 7, "2024-09-01", 90),
            order("buy", 10, "2024-09-02", 100),
            order("buy", 5, "2024-09-03", 110),
            order("sell", 12, "2024-09-04", 120),
            order("sell", 6, "2024-09-05", 130),
            order("buy", 4, "2024-09-06", 140),
        ]
        trades = pair_orders(orders)
        self.assertEqual(sum(t["quantity"] for t in trades if t["buy_date"]), 19)
        self.assertEqual(sum(t["quantity"] for t in trades if t["sell_date"]), 25)
        self.assertTrue(all(t["quantity"] > 0 for t in trades))
        self.assertTrue(all(t["buy_date"] <= t["sell_date"] for t in trades if t["buy_date"] and t["sell_date"]))

    def test_sell_before_buy_never_closes_later_purchase(self):
        trades = pair_orders([
            order("buy", 5, "2024-09-02"),
            order("sell", 5, "2024-09-01", 120),
        ])
        self.assertEqual(len(trades), 2)
        self.assertFalse(any(t["buy_price"] and t["sell_price"] for t in trades))

    def test_early_unmatched_sell_does_not_consume_future_buy(self):
        trades = pair_orders([
            order("sell", 5, "2024-09-01", 90),
            order("buy", 5, "2024-09-02", 100),
            order("sell", 3, "2024-09-03", 120),
        ])
        self.assertEqual([(t["quantity"], t["buy_price"], t["sell_price"]) for t in trades], [
            (5, "", 90), (3, 100, 120), (2, 100, ""),
        ])

    def test_sell_remainder_is_preserved(self):
        trades = pair_orders([order("buy", 2, "2024-09-01"), order("sell", 5, "2024-09-02", 120)])
        self.assertEqual([(t["quantity"], t["buy_price"], t["sell_price"]) for t in trades], [
            (2, 100, 120), (3, "", 120),
        ])

    def test_unknown_quantities_are_not_inferred_from_other_side(self):
        for buy_qty, sell_qty in ((None, 5), (5, None), (None, None)):
            with self.subTest(buy_qty=buy_qty, sell_qty=sell_qty):
                trades = pair_orders([
                    order("buy", buy_qty, "2024-09-01"),
                    order("sell", sell_qty, "2024-09-02", 120),
                ])
                self.assertEqual(len(trades), 2)
                self.assertFalse(any(t["buy_price"] and t["sell_price"] for t in trades))
                self.assertEqual(sum(t["quantity"] == "" for t in trades), (buy_qty is None) + (sell_qty is None))

    def test_unknown_dates_and_stocks_are_not_pairing_keys(self):
        for field, value in (
            ("date", None), ("date", "not-a-date"), ("symbol", ""),
            ("symbol", " "), ("symbol", None),
        ):
            with self.subTest(field=field, value=value):
                buy, sell = order("buy", 5, "2024-09-01"), order("sell", 5, "2024-09-02", 120)
                buy[field] = sell[field] = value
                trades = pair_orders([buy, sell])
                self.assertEqual(len(trades), 2)
                self.assertFalse(any(t["buy_price"] and t["sell_price"] for t in trades))

    def test_different_names_are_not_fuzzy_matched(self):
        trades = pair_orders([
            order("buy", 5, "2024-09-01", symbol="Reliance Industries"),
            order("sell", 5, "2024-09-02", symbol="RELIANCE"),
        ])
        self.assertEqual(len(trades), 2)

    def test_matching_normalizes_case_and_whitespace_only(self):
        trades = pair_orders([
            order("buy", 5, "2024-09-01", symbol="Reliance Industries"),
            order("sell", 5, "2024-09-02", symbol=" RELIANCE   INDUSTRIES "),
        ])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["stock"], "Reliance Industries")

    def test_missing_price_is_not_copied_from_other_side(self):
        trades = pair_orders([
            order("buy", 5, "2024-09-01", None),
            order("sell", 5, "2024-09-02", 120),
        ])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["buy_price"], "")
        self.assertEqual(trades[0]["sell_price"], 120)

    def test_same_day_explicit_times_prevent_reverse_pairing(self):
        result = parse("""
            INFY
            Buy Qty5
            Avg100
            Executed 12 Sep 2024 11:00:00
            INFY
            Sell Qty5
            Avg120
            Executed 12 Sep 2024 09:00:00
        """)
        self.assertEqual(len(result["trades"]), 2)
        self.assertFalse(any(t["buy_price"] and t["sell_price"] for t in result["trades"]))

    def test_same_day_explicit_times_match_in_time_order(self):
        result = parse("""
            INFY
            Sell Qty5
            Avg120
            Executed 12 Sep 2024 11:00:00
            INFY
            Buy Qty5
            Avg100
            Executed 12 Sep 2024 09:00:00
        """)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["buy_price"], 100)

    def test_malformed_times_do_not_establish_same_day_ordering(self):
        for clock in ("25:00:00", "09:99:00", "00:15 PM"):
            with self.subTest(clock=clock):
                result = parse_text(
                    f"INFY\nBUY Qty5\nAvg100\nExecuted 12 Sep 2024 {clock}\n"
                    "INFY\nSELL Qty5\nAvg120\nExecuted 12 Sep 2024")
                self.assertEqual(len(result["trades"]), 2)
                self.assertTrue(any("time is ambiguous" in warning for warning in result["warnings"]))

    def test_12_hour_times_are_compared_correctly(self):
        result = parse("""
            INFY
            SELL Qty5
            Avg120
            Executed 12 Sep 2024 12:15 PM
            INFY
            BUY Qty5
            Avg100
            Executed 12 Sep 2024 12:15 AM
        """)
        self.assertEqual(len(result["trades"]), 1)

    def test_same_day_one_missing_time_is_ambiguous(self):
        result = parse("""
            INFY
            Buy Qty5
            Avg100
            Executed 12 Sep 2024
            INFY
            Sell Qty5
            Avg120
            Executed 12 Sep 2024 09:00:00
        """)
        self.assertEqual(len(result["trades"]), 2)

    def test_known_dates_without_times_can_match(self):
        trades = pair_orders([
            order("sell", 5, "2024-09-12", 120),
            order("buy", 5, "2024-09-12", 100),
        ])
        self.assertEqual(len(trades), 1)

    def test_pairing_does_not_mutate_input(self):
        orders = [order("buy", 5, "2024-09-01"), order("sell", 2, "2024-09-02")]
        original = copy.deepcopy(orders)
        pair_orders(orders)
        self.assertEqual(orders, original)


if __name__ == "__main__":
    unittest.main()
