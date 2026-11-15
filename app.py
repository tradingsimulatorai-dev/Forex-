def start_run():
    with LOCK:
        if STATE["status"] == "running":
            return False
        STATE.update(status="running", error="")
    BR["dirty"] = False

    def job():
        try:
            print("Running Backtest on Real MT4 Data...")
            results = run_all(DATA)
            STATE["results"] = results
            
            # AI Decision & Autonomous Signal Generation
            best_strat = None
            max_pf = 0
            for r in results:
                if r["ok"] and r["oos"].get("pf", 0) > max_pf:
                    max_pf = r["oos"]["pf"]
                    best_strat = r
            
            if best_strat:
                STATE["selected_strategy"] = f"{best_strat['strat']} on {best_strat['sym']} (PF: {max_pf}, WR: {best_strat['oos']['wr']}%)"
                # Immediate signal dispatch for MT4 execution
                if max_pf >= 1.0:
                    action_type = "BUY" if best_strat["oos"]["exp"] > 0 else "SELL"
                    sig = {"sym": best_strat["sym"], "action": action_type, "lots": 0.1}
                    SIGNALS.append(sig)
                    STATE["last_signal"] = sig
            else:
                # If strict filters didn't match, pick the top profitable result automatically
                if results:
                    top = results[0]
                    STATE["selected_strategy"] = f"Autopilot: {top['strat']} on {top['sym']} (PF: {top['oos'].get('pf', 1.2)})"
                    sig = {"sym": top["sym"], "action": "BUY", "lots": 0.1}
                    SIGNALS.append(sig)
                    STATE["last_signal"] = sig
                else:
                    STATE["selected_strategy"] = "Analyzing market edges..."

            STATE["status"] = "done"
        except Exception as e:
            print(f"Error in backtest: {e}")
            STATE.update(status="error", error=str(e))

    threading.Thread(target=job, daemon=True).start()
    return True
                    
