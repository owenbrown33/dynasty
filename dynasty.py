import streamlit as st
import asyncio
import httpx
import time
from datetime import datetime, timedelta
from streamlit_float import *

# --- 1. config & theme ---
st.set_page_config(layout="wide", page_title="Leaguemate Trades")
float_init()

st.markdown(f"""
    <style>
    .stApp {{ background-color: #0E1117; color: #FFFFFF; }}
    .footer-bar-container {{ background-color: transparent !important; padding: 10px; }}
    .stProgress > div > div > div > div {{ background-color: #FFB6C1 !important; }}
    .stProgress div[data-testid="stWidgetLabel"] p {{
        font-family: sans-serif; font-size: 0.85rem; color: #FFB6C1;
        opacity: 0.8; margin-bottom: 4px; text-transform: lowercase;
    }}
    .stButton>button {{
        border: 1px solid #FFB6C1 !important; background-color: transparent !important;
        color: #FFB6C1 !important; border-radius: 4px; width: 100%; font-size: 0.9rem;
    }}
    .status-line {{
        padding: 20px; color: #90EE90; font-size: 1rem;
        border: 2px solid rgba(144, 238, 144, 0.4);
        background-color: rgba(144, 238, 144, 0.1);
        border-radius: 10px; margin: 20px 0px;
        text-align: center; font-weight: 600;
    }}
    .opp-tag-sell {{
        color: #FFB6C1; font-weight: 500; font-size: 0.7rem;
        border: 1px solid rgba(255, 182, 193, 0.3); padding: 1px 5px; border-radius: 3px;
        margin-left: 8px;
    }}
    .opp-tag-buy {{
        color: #90EE90; font-weight: 500; font-size: 0.7rem;
        border: 1px solid rgba(144, 238, 144, 0.3); padding: 1px 5px; border-radius: 3px;
        margin-left: 8px;
    }}
    </style>
    """, unsafe_allow_html=True)

API_BASE = "https://api.sleeper.app/v1"

# Session State Initialization
if 'found_mirrors' not in st.session_state: st.session_state.found_mirrors = []
if 'scan_finished' not in st.session_state: st.session_state.scan_finished = False
if 'num_mates' not in st.session_state: st.session_state.num_mates = None
if 'num_leagues' not in st.session_state: st.session_state.num_leagues = None
if 'num_opps' not in st.session_state: st.session_state.num_opps = None
if 'duration' not in st.session_state: st.session_state.duration = None

# --- 2. core logic ---
@st.cache_data(ttl=3600)
def get_all_players_map():
    try:
        resp = httpx.get(f"{API_BASE}/players/nfl", timeout=60.0)
        return resp.json() if resp.status_code == 200 else {}
    except: return {}

async def fetch_with_progress(client, url, sem, counter, total, prog_bar, base_pct, span_pct, time_slot, start_t):
    async with sem:
        try:
            # Live duration update during fetch
            st.session_state.duration = f"{time.perf_counter() - start_t:.1f}s"
            time_slot.metric("duration", st.session_state.duration)

            resp = await client.get(url)
            counter[0] += 1
            current_pct = base_pct + (span_pct * (counter[0] / total))
            prog_bar.progress(min(current_pct, 0.99), text=f"fetching {counter[0]} of {total} leagues...")
            return resp.json() if resp.status_code == 200 else None
        except:
            counter[0] += 1
            return None

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
                st.markdown(f"**@{m['Name']}**")
                for item in m['Adds']:
                    lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                    st.markdown(f"\+ {item['n']}{lbl}", unsafe_allow_html=True)
                for item in m['Drops']:
                    lbl = f"<span class='{item['c']}'>{item['l']}</span>" if item['l'] else ""
                    st.markdown(f"\- {item['n']}{lbl}", unsafe_allow_html=True)

async def run_scanner(username, slots, lookback_days, live_area, progress_placeholder):
    # Order: duration, num_mates, num_leagues, num_opps
    m_time_p, m_mates_p, m_leagues_p, m_opps_p = slots
    sem = asyncio.Semaphore(50)
    players_data = get_all_players_map()
    start_time = time.perf_counter()

    with progress_placeholder:
        prog_bar = st.progress(0, text="initializing...")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Initial live timer kick-off
        st.session_state.duration = "0.1s"
        m_time_p.metric("duration", st.session_state.duration)

        user_r = await client.get(f"{API_BASE}/user/{username}")
        if user_r.status_code != 200:
            progress_placeholder.empty()
            return
        my_uid = user_r.json()["user_id"]
        state = (await client.get(f"{API_BASE}/state/nfl")).json()
        my_leagues = (await client.get(f"{API_BASE}/user/{my_uid}/leagues/nfl/{state['season']}")).json() or []

        my_p, mate_p, mates_l = {}, {}, {}
        prog_bar.progress(0.1, text="mapping leaguemates...")
        ctx_res = await asyncio.gather(*[get_league_maps(client, lg['league_id'], sem) for lg in my_leagues])
        rost_res = await asyncio.gather(*[fetch_json(client, f"{API_BASE}/league/{lg['league_id']}/rosters", sem) for lg in my_leagues])

        for lg, r_list, l_ctx in zip(my_leagues, rost_res, ctx_res):
            lid = lg["league_id"]
            if not r_list or not l_ctx: continue
            for r in r_list:
                uid = r.get("owner_id")
                if not uid: continue
                if uid == my_uid:
                    for pid in (r.get("players") or []): my_p.setdefault(pid, set()).add(lid)
                else:
                    mates_l.setdefault(uid, set()).add(lid)
                    mate_p.setdefault(uid, {})
                    for pid in (r.get("players") or []): mate_p[uid].setdefault(pid, set()).add(lid)

        st.session_state.num_mates = len(mates_l)
        m_mates_p.metric("leaguemates", st.session_state.num_mates)

        prog_bar.progress(0.2, text="building trade scan...")
        mate_res = await asyncio.gather(*[fetch_json(client, f"{API_BASE}/user/{mid}/leagues/nfl/{state['season']}", sem) for mid in mates_l.keys()])
        target_ids = list({l["league_id"] for res in mate_res if res for l in res})
        st.session_state.num_leagues = len(target_ids)
        m_leagues_p.metric("leagues", st.session_state.num_leagues)

        cutoff = (datetime.now() - timedelta(days=lookback_days)).timestamp() * 1000
        weeks = [state.get('week', 1), state.get('week', 1)-1] if state.get('week', 1) > 1 else [1]
        fetch_counter = [0]
        trade_tasks, task_info = [], []
        for lid in target_ids:
            for w in weeks:
                url = f"{API_BASE}/league/{lid}/transactions/{w}"
                trade_tasks.append(fetch_with_progress(client, url, sem, fetch_counter, len(target_ids) * len(weeks), prog_bar, 0.2, 0.4, m_time_p, start_time))
                task_info.append((lid, w))

        all_txns = await asyncio.gather(*trade_tasks)
        total_steps = len(all_txns)
        for i, (txns, (lid, w)) in enumerate(zip(all_txns, task_info)):
            current_pct = 0.6 + (0.4 * (i+1)/total_steps)
            prog_bar.progress(current_pct, text=f"scanning {i+1} of {total_steps} leagues...")

            # Live duration update
            st.session_state.duration = f"{time.perf_counter() - start_time:.1f}s"
            m_time_p.metric("duration", st.session_state.duration)

            if not txns: continue
            l_ctx = None
            for t in txns:
                if t.get("type") != "trade" or t.get("status_updated") < cutoff: continue
                if not l_ctx: l_ctx = await get_league_maps(client, lid, sem)
                if not l_ctx: continue
                mgr_data, hit = {}, False
                involved_rids = set((t.get("adds") or {}).values()) | set((t.get("drops") or {}).values())
                for dp in (t.get("draft_picks") or []): involved_rids.add(dp["owner_id"]); involved_rids.add(dp["previous_owner_id"])
                for fb in (t.get("waiver_budget") or []): involved_rids.add(fb["sender"]); involved_rids.add(fb["receiver"])
                uids = {l_ctx["rid"].get(rid) for rid in involved_rids if l_ctx["rid"].get(rid)}
                for u in uids: mgr_data[u] = {"adds":[], "drops":[]}

                for pid, rid in (t.get("adds") or {}).items():
                    u = l_ctx["rid"].get(rid)
                    if not u: continue
                    lbl, cls = "", ""
                    if pid in my_p and u in mates_l:
                        shared = my_p[pid].intersection(mates_l[u])
                        if shared: lbl, cls, hit = f"Sell opportunity ({', '.join([st.session_state.league_cache[s]['name'] for s in shared])})", "opp-tag-sell", True
                    mgr_data.setdefault(u, {"adds":[], "drops":[]})["adds"].append({"n": players_data.get(pid,{}).get('full_name', pid), "l": lbl, "c": cls})

                for pid, rid in (t.get("drops") or {}).items():
                    u = l_ctx["rid"].get(rid)
                    if not u: continue
                    lbl, cls = "", ""
                    if u in mate_p and pid in mate_p[u]:
                        shared = mate_p[u][pid].intersection(mates_l[u])
                        if shared: lbl, cls, hit = f"Buy opportunity ({', '.join([st.session_state.league_cache[s]['name'] for s in shared])})", "opp-tag-buy", True
                    mgr_data.setdefault(u, {"adds":[], "drops":[]})["drops"].append({"n": players_data.get(pid,{}).get('full_name', pid), "l": lbl, "c": cls})

                for dp in (t.get("draft_picks") or []):
                    slot = l_ctx["roster_to_slot"].get(dp["roster_id"], "?")
                    p_name = f"{dp['season']} {dp['round']}.{slot:02d}" if (dp['season'] == "2026" and slot != "?") else f"{dp['season']} Round {dp['round']}"
                    tu, gu = l_ctx["rid"].get(dp["owner_id"]), l_ctx["rid"].get(dp["previous_owner_id"])
                    if tu: mgr_data.setdefault(tu, {"adds":[], "drops":[]})["adds"].append({"n": p_name, "l": "", "c": ""})
                    if gu: mgr_data.setdefault(gu, {"adds":[], "drops":[]})["drops"].append({"n": p_name, "l": "", "c": ""})

                for fb in (t.get("waiver_budget") or []):
                    amount = f"${fb['amount']} faab"
                    su, ru = l_ctx["rid"].get(fb["sender"]), l_ctx["rid"].get(fb["receiver"])
                    if ru: mgr_data.setdefault(ru, {"adds":[], "drops":[]})["adds"].append({"n": amount, "l": "", "c": ""})
                    if su: mgr_data.setdefault(su, {"adds":[], "drops":[]})["drops"].append({"n": amount, "l": "", "c": ""})

                if hit:
                    trade_obj = {"League": l_ctx["name"], "Time": datetime.fromtimestamp(t["status_updated"]/1000).strftime("%m/%d %H:%M"), "Managers": [{"Name": l_ctx["uid"].get(u, "unknown"), "Adds": d["adds"], "Drops": d["drops"]} for u, d in mgr_data.items() if u]}
                    st.session_state.found_mirrors.append(trade_obj)
                    st.session_state.num_opps = len(st.session_state.found_mirrors)
                    m_opps_p.metric("opportunities identified", st.session_state.num_opps)
                    with live_area: render_trade(trade_obj)

        progress_placeholder.empty()
        st.session_state.scan_finished = True
        st.rerun()

def main():
    st.title("Leaguemate Trades")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1: user = st.text_input("sleeper username", value="browntown333")
        with col2: days = st.number_input("lookback days", min_value=1, max_value=30, value=14)
        run_btn = st.button("Run", type="primary", use_container_width=True)

    # Metric Slots with Duration First
    m_cols = st.columns(4)
    m_time_p = m_cols[0].empty()
    m_mates_p = m_cols[1].empty()
    m_leagues_p = m_cols[2].empty()
    m_opps_p = m_cols[3].empty()
    slots = [m_time_p, m_mates_p, m_leagues_p, m_opps_p]

    if st.session_state.duration is not None: m_time_p.metric("duration", st.session_state.duration)
    if st.session_state.num_mates is not None: m_mates_p.metric("leaguemates", st.session_state.num_mates)
    if st.session_state.num_leagues is not None: m_leagues_p.metric("leagues", st.session_state.num_leagues)
    if st.session_state.num_opps is not None: m_opps_p.metric("opportunities identified", st.session_state.num_opps)

    live_area = st.container()

    footer_container = st.container()
    with footer_container:
        st.markdown('<div class="footer-bar-container">', unsafe_allow_html=True)
        progress_placeholder = st.empty()
        st.markdown('</div>', unsafe_allow_html=True)
    footer_container.float("bottom: 20px; left: 50%; width: 60%; transform: translateX(-50%); z-index: 9999;")

    if not run_btn:
        for t in reversed(st.session_state.found_mirrors):
            render_trade(t)
    else:
        st.session_state.found_mirrors = []
        st.session_state.scan_finished = False
        st.session_state.num_mates = 0
        st.session_state.num_leagues = 0
        st.session_state.num_opps = 0
        st.session_state.duration = "0.0s"
        asyncio.run(run_scanner(user, slots, days, live_area, progress_placeholder))

    if st.session_state.scan_finished and st.session_state.found_mirrors:
        st.markdown(f'<div class="status-line">Scan complete — {len(st.session_state.found_mirrors)} opportunities found</div>', unsafe_allow_html=True)

        csv_rows = ["Time,League,Manager 1,Manager 1 Moves,Manager 2,Manager 2 Moves"]
        for t in st.session_state.found_mirrors:
            m_list = t['Managers']
            m1_name = f"@{m_list[0]['Name']}" if len(m_list) > 0 else ""
            m1_moves = [f"+ {x['n']} ({x['l']})" if x['l'] else f"+ {x['n']}" for x in m_list[0]['Adds']] + \
                       [f"- {x['n']} ({x['l']})" if x['l'] else f"- {x['n']}" for x in m_list[0]['Drops']] if len(m_list) > 0 else []
            m2_name = f"@{m_list[1]['Name']}" if len(m_list) > 1 else ""
            m2_moves = [f"+ {x['n']} ({x['l']})" if x['l'] else f"+ {x['n']}" for x in m_list[1]['Adds']] + \
                       [f"- {x['n']} ({x['l']})" if x['l'] else f"- {x['n']}" for x in m_list[1]['Drops']] if len(m_list) > 1 else []
            if len(m_list) > 2:
                m2_name += " (Multi-Team)"
                for extra in m_list[2:]:
                    m2_moves.append(f"--- @{extra['Name']} moves ---")
                    m2_moves += [f"+ {x['n']} ({x['l']})" if x['l'] else f"+ {x['n']}" for x in extra['Adds']]
                    m2_moves += [f"- {x['n']} ({x['l']})" if x['l'] else f"- {x['n']}" for x in extra['Drops']]
            csv_rows.append(f'"{t["Time"]}","{t["League"]}","{m1_name}","{" | ".join(m1_moves)}","{m2_name}","{" | ".join(m2_moves)}"')

        st.download_button(
            label="📥 Download CSV",
            data="\n".join(csv_rows),
            file_name=f"trades_{datetime.now().strftime('%m%d_%H%M')}.csv",
            mime="text/csv",
            use_container_width=True,
            key="persistent_download"
        )

if __name__ == "__main__": main()
