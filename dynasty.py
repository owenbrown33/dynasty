import streamlit as st
import asyncio
import httpx
import time
from datetime import datetime, timedelta

# --- 1. CONFIG & THEME (PINK PROGRESS BAR PERMANENT) ---
st.set_page_config(layout="wide", page_title="Leaguemate Trades")

st.markdown(f"""
    <style>
    .stApp {{ background-color: #0E1117; color: #FFFFFF; }}
    /* TARGET PINK PROGRESS BAR - PERMANENT */
    .stProgress > div > div > div > div {{ background-color: #FFB6C1 !important; }}

    .stButton>button {{
        border: 2px solid #FFB6C1 !important; background-color: transparent !important;
        color: #FFB6C1 !important; border-radius: 8px; width: 100%; font-weight: bold;
    }}
    /* PINK TAG FOR SELLING */
    .opp-tag-sell {{
        color: #FFB6C1; font-weight: bold; font-size: 0.75rem;
        border: 1px solid rgba(255, 182, 193, 0.3); padding: 2px 6px; border-radius: 4px;
        margin-left: 8px; vertical-align: middle;
    }}
    /* GREEN TAG FOR BUYING */
    .opp-tag-buy {{
        color: #90EE90; font-weight: bold; font-size: 0.75rem;
        border: 1px solid rgba(144, 238, 144, 0.3); padding: 2px 6px; border-radius: 4px;
        margin-left: 8px; vertical-align: middle;
    }}
    </style>
    """, unsafe_allow_html=True)

API_BASE = "https://api.sleeper.app/v1"

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

    tasks = [
        fetch_json(client, f"{API_BASE}/league/{lid}", sem),
        fetch_json(client, f"{API_BASE}/league/{lid}/rosters", sem),
        fetch_json(client, f"{API_BASE}/league/{lid}/users", sem),
        fetch_json(client, f"{API_BASE}/league/{lid}/drafts", sem)
    ]
    l_info, r_data, u_data, d_ids = await asyncio.gather(*tasks)
    if not l_info or not r_data: return None

    rid_to_uid = {r['roster_id']: r.get('owner_id') for r in r_data}
    uid_to_name = {u['user_id']: u.get('display_name', u['user_id']) for u in (u_data or [])}

    roster_to_slot = {}
    if d_ids and len(d_ids) > 0:
        draft_details = await fetch_json(client, f"{API_BASE}/draft/{d_ids[0]['draft_id']}", sem)
        if draft_details and draft_details.get("draft_order"):
            draft_order = draft_details["draft_order"]
            for r_id, u_id in rid_to_uid.items():
                if u_id in draft_order:
                    roster_to_slot[r_id] = draft_order[u_id]

    ctx = {"name": l_info.get("name"), "rid": rid_to_uid, "uid": uid_to_name, "roster_to_slot": roster_to_slot}
    st.session_state.league_cache[lid] = ctx
    return ctx

async def run_scanner(username, stat_slots, lookback_days):
    sem = asyncio.Semaphore(50)
    players_data = get_all_players_map()
    m_mates, m_leagues, m_trades, m_time = stat_slots
    start_time = time.perf_counter()

    prog_bar = st.progress(0, text="Step 1/4: Initializing connection...")

    async with httpx.AsyncClient(timeout=20.0) as client:
        with st.spinner("Accessing Sleeper ecosystem..."):
            user_r = await client.get(f"{API_BASE}/user/{username}")
            if user_r.status_code != 200:
                prog_bar.empty()
                return []
            my_uid = user_r.json()["user_id"]
            state = (await client.get(f"{API_BASE}/state/nfl")).json()
            my_leagues_raw = (await client.get(f"{API_BASE}/user/{my_uid}/leagues/nfl/{state['season']}")).json() or []

            my_players, mate_players, my_picks, mate_picks, mates_leagues = {}, {}, {}, {}, {}

            prog_bar.progress(0.1, text="Step 1/4: Mapping your roster ownership...")
            roster_reqs = [fetch_json(client, f"{API_BASE}/league/{lg['league_id']}/rosters", sem) for lg in my_leagues_raw]
            ctx_reqs = [get_league_maps(client, lg['league_id'], sem) for lg in my_leagues_raw]

            roster_results = await asyncio.gather(*roster_reqs)
            ctx_results = await asyncio.gather(*ctx_reqs)

            for lg, r_list, l_ctx in zip(my_leagues_raw, roster_results, ctx_results):
                lid = lg["league_id"]
                if not r_list or not l_ctx: continue
                for r in r_list:
                    uid = r.get("owner_id")
                    if not uid: continue
                    slot = l_ctx["roster_to_slot"].get(r["roster_id"])

                    if uid == my_uid:
                        for pid in (r.get("players") or []): my_players.setdefault(pid, set()).add(lid)
                        if slot:
                            for rd in range(1, 5): my_picks.setdefault(("2026", rd, slot), set()).add(lid)
                    else:
                        mates_leagues.setdefault(uid, set()).add(lid)
                        mate_players.setdefault(uid, {})
                        for pid in (r.get("players") or []):
                            mate_players[uid].setdefault(pid, set()).add(lid)
                        if slot:
                            mate_picks.setdefault(uid, {})
                            for rd in range(1, 5):
                                mate_picks[uid].setdefault(("2026", rd, slot), set()).add(lid)

        m_mates.metric("Leaguemates", len(mates_leagues))

        prog_bar.progress(0.2, text="Step 2/4: Identifying leaguemate activity...")
        mate_tasks = [fetch_json(client, f"{API_BASE}/user/{mid}/leagues/nfl/{state['season']}", sem) for mid in mates_leagues.keys()]
        mate_res = await asyncio.gather(*mate_tasks)
        target_ids = {l["league_id"] for res in mate_res if res for l in res}
        m_leagues.metric("Total Scanned", len(target_ids))

        prog_bar.progress(0.3, text=f"Step 3/4: Fetching trades from {len(target_ids)} leagues...")
        cutoff_ts = (datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000
        weeks = [state.get('week', 1), state.get('week', 1)-1] if state.get('week', 1) > 1 else [1]

        trade_tasks, task_info = [], []
        for lid in target_ids:
            for w in weeks:
                trade_tasks.append(fetch_json(client, f"{API_BASE}/league/{lid}/transactions/{w}", sem))
                task_info.append((lid, w))

        final_trades = []
        all_txns_results = await asyncio.gather(*trade_tasks)
        total_batches = len(all_txns_results)

        for i, (txns, (lid, w)) in enumerate(zip(all_txns_results, task_info)):
            current_pct = 0.3 + (0.7 * (i + 1) / total_batches)
            prog_bar.progress(current_pct, text=f"Step 4/4: Analyzing League {i+1}/{total_batches}...")

            if not txns: continue
            l_ctx = None
            for t in txns:
                if t.get("type") != "trade" or t.get("status_updated") < cutoff_ts: continue
                if not l_ctx: l_ctx = await get_league_maps(client, lid, sem)
                if not l_ctx: continue

                mgr_data, has_hit = {}, False
                all_roster_ids = set((t.get("adds") or {}).values()) | set((t.get("drops") or {}).values())
                all_uids = {l_ctx["rid"].get(rid) for rid in all_roster_ids if l_ctx["rid"].get(rid)}

                for uid in all_uids: mgr_data[uid] = {"adds": [], "drops": []}

                # Players Analysis
                for p_id, r_id in (t.get("adds") or {}).items():
                    uid = l_ctx["rid"].get(r_id)
                    lbl, cls = "", ""
                    if p_id in my_players and uid in mates_leagues:
                        shared = my_players[p_id].intersection(mates_leagues[uid])
                        if shared:
                            l_names = [st.session_state.league_cache[sid]['name'] for sid in shared]
                            lbl = f"Sell Opportunity ({', '.join(l_names)})"
                            cls = "opp-tag-sell"
                            has_hit = True
                    mgr_data[uid]["adds"].append({"n": players_data.get(p_id,{}).get('full_name', p_id), "l": lbl, "c": cls})

                for p_id, r_id in (t.get("drops") or {}).items():
                    uid = l_ctx["rid"].get(r_id)
                    lbl, cls = "", ""
                    if uid in mate_players and p_id in mate_players[uid]:
                        shared = mate_players[uid][p_id].intersection(mates_leagues[uid])
                        if shared:
                            l_names = [st.session_state.league_cache[sid]['name'] for sid in shared]
                            lbl = f"Buy Opportunity ({', '.join(l_names)})"
                            cls = "opp-tag-buy"
                            has_hit = True
                    mgr_data[uid]["drops"].append({"n": players_data.get(p_id,{}).get('full_name', p_id), "l": lbl, "c": cls})

                # Picks Analysis
                for dp in (t.get("draft_picks") or []):
                    slot = l_ctx["roster_to_slot"].get(dp["roster_id"], "?")
                    p_name = f"{dp['season']} {dp['round']}.{slot:02d}" if (dp['season'] == "2026" and slot != "?") else f"{dp['season']} Rd {dp['round']}"
                    t_uid, g_uid = l_ctx["rid"].get(dp["owner_id"]), l_ctx["rid"].get(dp["previous_owner_id"])
                    pick_key = (str(dp['season']), int(dp['round']), slot)

                    lbl_t, cls_t = "", ""
                    if pick_key in my_picks and t_uid in mates_leagues:
                        shared = my_picks[pick_key].intersection(mates_leagues[t_uid])
                        if shared:
                            l_names = [st.session_state.league_cache[sid]['name'] for sid in shared]
                            lbl_t = f"Sell Opportunity ({', '.join(l_names)})"
                            cls_t = "opp-tag-sell"; has_hit = True
                    if t_uid in mgr_data: mgr_data[t_uid]["adds"].append({"n": p_name, "l": lbl_t, "c": cls_t})

                    lbl_g, cls_g = "", ""
                    if g_uid in mate_picks and pick_key in mate_picks[g_uid]:
                        shared = mate_picks[g_uid][pick_key].intersection(mates_leagues[g_uid])
                        if shared:
                            l_names = [st.session_state.league_cache[sid]['name'] for sid in shared]
                            lbl_g = f"Buy Opportunity ({', '.join(l_names)})"
                            cls_g = "opp-tag-buy"; has_hit = True
                    if g_uid in mgr_data: mgr_data[g_uid]["drops"].append({"n": p_name, "l": lbl_g, "c": cls_g})

                if has_hit and mgr_data:
                    final_trades.append({
                        "League": l_ctx["name"],
                        "Time": datetime.fromtimestamp(t["status_updated"]/1000).strftime("%m/%d %H:%M"),
                        "Managers": [{"Name": l_ctx["uid"].get(u, "Unknown"), "Adds": d["adds"], "Drops": d["drops"]} for u, d in mgr_data.items() if u]
                    })
                    m_trades.metric("Mirrors Found", len(final_trades))
        prog_bar.empty()
    m_time.metric("Time", f"{time.perf_counter() - start_time:.1f}s")
    return final_trades

def main():
    st.title("Leaguemate Trades")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1: user = st.text_input("Sleeper Username", value="browntown333")
        with col2: days = st.number_input("Lookback Days", min_value=1, max_value=30, value=14)
        run_btn = st.button("Start Mirror Scan", type="primary")

    slots = [c.empty() for c in st.columns(4)]
    if run_btn:
        results = asyncio.run(run_scanner(user, slots, days))
        for t in sorted(results, key=lambda x: x['Time'], reverse=True):
            with st.container(border=True):
                st.subheader(f"{t['League']} — {t['Time']}")
                num_managers = len(t['Managers'])
                if num_managers > 0:
                    cols = st.columns(num_managers)
                    for i, m in enumerate(t['Managers']):
                        with cols[i]:
                            st.markdown(f"**{m['Name']}**")
                            for item in m['Adds']:
                                lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                                st.markdown(f"\+ {item['n']}{lbl}", unsafe_allow_html=True)
                            for item in m['Drops']:
                                lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                                st.markdown(f"\- {item['n']}{lbl}", unsafe_allow_html=True)

if __name__ == "__main__": main()
