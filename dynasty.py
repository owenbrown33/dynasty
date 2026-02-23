import streamlit as st
import asyncio
import httpx
import time
from datetime import datetime, timedelta

# --- 1. CONFIG & THEME ---
st.set_page_config(layout="wide", page_title="Leaguemate Trades")

st.markdown(f"""
    <style>
    .stApp {{ background-color: #0E1117; color: #FFFFFF; }}
    .stProgress > div > div > div > div {{ background-color: #FFB6C1 !important; }}
    .stButton>button {{
        border: 2px solid #FFB6C1 !important; background-color: transparent !important;
        color: #FFB6C1 !important; border-radius: 8px; width: 100%; font-weight: bold;
    }}
    .opp-tag-sell {{
        color: #FFB6C1; font-weight: bold; font-size: 0.75rem;
        border: 1px solid rgba(255, 182, 193, 0.3); padding: 2px 6px; border-radius: 4px;
        margin-left: 8px; vertical-align: middle;
    }}
    .opp-tag-buy {{
        color: #90EE90; font-weight: bold; font-size: 0.75rem;
        border: 1px solid rgba(144, 238, 144, 0.3); padding: 2px 6px; border-radius: 4px;
        margin-left: 8px; vertical-align: middle;
    }}
    </style>
    """, unsafe_allow_html=True)

API_BASE = "https://api.sleeper.app/v1"

# --- 2. PERSISTENCE INITIALIZATION ---
if 'found_mirrors' not in st.session_state:
    st.session_state.found_mirrors = []

# --- 3. CORE LOGIC ---
@st.cache_data(ttl=3600)
def get_all_players_map():
    try:
        resp = httpx.get(f"{API_BASE}/players/nfl", timeout=60.0)
        return resp.json() if resp.status_code == 200 else {}
    except: return {}

async def fetch_json(client, url, sem):
    async with sem:
        try:
            resp = await client.get(url)
            return resp.json() if resp.status_code == 200 else None
        except: return None

async def get_league_maps(client, lid, sem):
    if 'league_cache' not in st.session_state: st.session_state.league_cache = {}
    if lid in st.session_state.league_cache: return st.session_state.league_cache[lid]

    tasks = [fetch_json(client, f"{API_BASE}/league/{lid}", sem),
             fetch_json(client, f"{API_BASE}/league/{lid}/rosters", sem),
             fetch_json(client, f"{API_BASE}/league/{lid}/users", sem),
             fetch_json(client, f"{API_BASE}/league/{lid}/drafts", sem)]
    res = await asyncio.gather(*tasks)
    l_info, r_data, u_data, d_ids = res[0], res[1], res[2], res[3]
    if not l_info or not r_data: return None

    rid_to_uid = {r['roster_id']: r.get('owner_id') for r in r_data}
    uid_to_name = {u['user_id']: u.get('display_name', u['user_id']) for u in (u_data or [])}
    roster_to_slot = {}
    if d_ids:
        details = await fetch_json(client, f"{API_BASE}/draft/{d_ids[0]['draft_id']}", sem)
        if details and details.get("draft_order"):
            for r_id, u_id in rid_to_uid.items():
                if u_id in details["draft_order"]: roster_to_slot[r_id] = details["draft_order"][u_id]

    ctx = {"name": l_info.get("name"), "rid": rid_to_uid, "uid": uid_to_name, "roster_to_slot": roster_to_slot}
    st.session_state.league_cache[lid] = ctx
    return ctx

def render_trade(t):
    with st.container(border=True):
        st.subheader(f"{t['League']} — {t['Time']}")
        cols = st.columns(len(t['Managers']))
        for i, m in enumerate(t['Managers']):
            with cols[i]:
                st.markdown(f"**{m['Name']}**")
                for item in m['Adds']:
                    lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                    st.markdown(f"\+ {item['n']}{lbl}", unsafe_allow_html=True)
                for item in m['Drops']:
                    lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                    st.markdown(f"\- {item['n']}{lbl}", unsafe_allow_html=True)

async def run_scanner(username, stat_slots, lookback_days, live_area):
    sem = asyncio.Semaphore(50)
    players_data = get_all_players_map()
    m_mates, m_leagues, m_trades, m_time = stat_slots
    start_time = time.perf_counter()
    prog_bar = st.progress(0, text="Initializing...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        user_r = await client.get(f"{API_BASE}/user/{username}")
        if user_r.status_code != 200: return
        my_uid = user_r.json()["user_id"]
        state = (await client.get(f"{API_BASE}/state/nfl")).json()
        my_leagues = (await client.get(f"{API_BASE}/user/{my_uid}/leagues/nfl/{state['season']}")).json() or []

        my_p, mate_p, my_pk, mate_pk, mates_l = {}, {}, {}, {}, {}

        # Step 1: Mapping
        prog_bar.progress(0.1, text="Step 1/4: Mapping ownership...")
        ctx_res = await asyncio.gather(*[get_league_maps(client, lg['league_id'], sem) for lg in my_leagues])
        rost_res = await asyncio.gather(*[fetch_json(client, f"{API_BASE}/league/{lg['league_id']}/rosters", sem) for lg in my_leagues])

        for lg, r_list, l_ctx in zip(my_leagues, rost_res, ctx_res):
            lid = lg["league_id"]
            if not r_list or not l_ctx: continue
            for r in r_list:
                uid = r.get("owner_id")
                if not uid: continue
                slot = l_ctx["roster_to_slot"].get(r["roster_id"])
                if uid == my_uid:
                    for pid in (r.get("players") or []): my_p.setdefault(pid, set()).add(lid)
                    if slot:
                        for rd in range(1, 5): my_pk.setdefault(("2026", rd, slot), set()).add(lid)
                else:
                    mates_l.setdefault(uid, set()).add(lid)
                    mate_p.setdefault(uid, {})
                    for pid in (r.get("players") or []): mate_p[uid].setdefault(pid, set()).add(lid)
                    if slot:
                        mate_pk.setdefault(uid, {})
                        for rd in range(1, 5): mate_pk[uid].setdefault(("2026", rd, slot), set()).add(lid)

        m_mates.metric("Leaguemates", len(mates_l))

        # Step 2: Activity
        prog_bar.progress(0.2, text="Step 2/4: Identifying activity...")
        mate_res = await asyncio.gather(*[fetch_json(client, f"{API_BASE}/user/{mid}/leagues/nfl/{state['season']}", sem) for mid in mates_l.keys()])
        target_ids = {l["league_id"] for res in mate_res if res for l in res}
        m_leagues.metric("Total Scanned", len(target_ids))

        # Step 3: Fetch Trades
        prog_bar.progress(0.3, text="Step 3/4: Fetching trades...")
        cutoff = (datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000
        weeks = [state.get('week', 1), state.get('week', 1)-1] if state.get('week', 1) > 1 else [1]

        trade_tasks, task_info = [], []
        for lid in target_ids:
            for w in weeks:
                trade_tasks.append(fetch_json(client, f"{API_BASE}/league/{lid}/transactions/{w}", sem))
                task_info.append((lid, w))

        all_txns = await asyncio.gather(*trade_tasks)

        # Step 4: Analyze
        for i, (txns, (lid, w)) in enumerate(zip(all_txns, task_info)):
            prog_bar.progress(0.3 + (0.7 * (i+1)/len(all_txns)), text=f"Analyzing League {i+1}/{total_batches}...")
            if not txns: continue
            l_ctx = None
            for t in txns:
                if t.get("type") != "trade" or t.get("status_updated") < cutoff: continue
                if not l_ctx: l_ctx = await get_league_maps(client, lid, sem)
                if not l_ctx: continue

                mgr_data, hit = {}, False
                uids = {l_ctx["rid"].get(rid) for rid in (set((t.get("adds") or {}).values()) | set((t.get("drops") or {}).values())) if l_ctx["rid"].get(rid)}
                for u in uids: mgr_data[u] = {"adds":[], "drops":[]}

                # Players
                for pid, rid in (t.get("adds") or {}).items():
                    u = l_ctx["rid"].get(rid)
                    if not u: continue
                    lbl, cls = "", ""
                    if pid in my_p and u in mates_l:
                        shared = my_p[pid].intersection(mates_l[u])
                        if shared: lbl, cls, hit = f"Sell Opp ({', '.join([st.session_state.league_cache[s]['name'] for s in shared])})", "opp-tag-sell", True
                    mgr_data.setdefault(u, {"adds":[], "drops":[]})["adds"].append({"n": players_data.get(pid,{}).get('full_name', pid), "l": lbl, "c": cls})

                for pid, rid in (t.get("drops") or {}).items():
                    u = l_ctx["rid"].get(rid)
                    if not u: continue
                    lbl, cls = "", ""
                    if u in mate_p and pid in mate_p[u]:
                        shared = mate_p[u][pid].intersection(mates_l[u])
                        if shared: lbl, cls, hit = f"Buy Opp ({', '.join([st.session_state.league_cache[s]['name'] for s in shared])})", "opp-tag-buy", True
                    mgr_data.setdefault(u, {"adds":[], "drops":[]})["drops"].append({"n": players_data.get(pid,{}).get('full_name', pid), "l": lbl, "c": cls})

                # Picks
                for dp in (t.get("draft_picks") or []):
                    slot = l_ctx["roster_to_slot"].get(dp["roster_id"], "?")
                    p_name = f"{dp['season']} {dp['round']}.{slot:02d}" if (dp['season'] == "2026" and slot != "?") else f"{dp['season']} Rd {dp['round']}"
                    tu, gu = l_ctx["rid"].get(dp["owner_id"]), l_ctx["rid"].get(dp["previous_owner_id"])
                    pk = (str(dp['season']), int(dp['round']), slot)
                    if tu:
                        l, c = "", ""
                        if pk in my_pk and tu in mates_l:
                            sh = my_pk[pk].intersection(mates_l[tu])
                            if sh: l, c, hit = f"Sell Opp ({', '.join([st.session_state.league_cache[s]['name'] for s in sh])})", "opp-tag-sell", True
                        mgr_data.setdefault(tu, {"adds":[], "drops":[]})["adds"].append({"n": p_name, "l": l, "c": c})
                    if gu:
                        l, c = "", ""
                        if gu in mate_pk and pk in mate_pk[gu]:
                            sh = mate_pk[gu][pk].intersection(mates_l[gu])
                            if sh: l, c, hit = f"Buy Opp ({', '.join([st.session_state.league_cache[s]['name'] for s in sh])})", "opp-tag-buy", True
                        mgr_data.setdefault(gu, {"adds":[], "drops":[]})["drops"].append({"n": p_name, "l": l, "c": c})

                if hit:
                    trade_obj = {"League": l_ctx["name"], "Time": datetime.fromtimestamp(t["status_updated"]/1000).strftime("%m/%d %H:%M"), "Managers": [{"Name": l_ctx["uid"].get(u, "Unknown"), "Adds": d["adds"], "Drops": d["drops"]} for u, d in mgr_data.items() if u]}
                    st.session_state.found_mirrors.append(trade_obj)
                    m_trades.metric("Mirrors Found", len(st.session_state.found_mirrors))
                    with live_area: render_trade(trade_obj)
        prog_bar.empty()
    m_time.metric("Time", f"{time.perf_counter() - start_time:.1f}s")

def main():
    st.title("Leaguemate Trades")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1: user = st.text_input("Sleeper Username", value="browntown333")
        with col2: days = st.number_input("Lookback Days", min_value=1, max_value=30, value=14)

        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: run_btn = st.button("Start Mirror Scan", type="primary")
        with c2:
            if st.button("🗑️ Clear Results"):
                st.session_state.found_mirrors = []
                st.rerun()
        with c3:
            if st.session_state.found_mirrors:
                csv = "League,Time,Manager,Adds,Drops\n"
                for t in st.session_state.found_mirrors:
                    for m in t['Managers']:
                        a = " | ".join([x['n'] for x in m['Adds']])
                        d = " | ".join([x['n'] for x in m['Drops']])
                        csv += f"{t['League']},{t['Time']},{m['Name']},{a},{d}\n"
                st.download_button("📥 Download", csv, "trades.csv", "text/csv")

    slots = [c.empty() for c in st.columns(4)]
    live_area = st.container()

    # Always show past results from session state
    if not run_btn:
        for t in reversed(st.session_state.found_mirrors):
            render_trade(t)
    else:
        st.session_state.found_mirrors = [] # Clear for new run
        asyncio.run(run_scanner(user, slots, days, live_area))

if __name__ == "__main__": main()
