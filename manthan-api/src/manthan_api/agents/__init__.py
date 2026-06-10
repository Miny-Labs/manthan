"""manthan_api.agents - the three macro A2A agent services.

Agents ARE the system. Each module here is a self-contained FastAPI app,
runnable as its own Cloud Run service from the same image (different
--command/--args), each with its own AgentCard + Agent Identity:

  triage.py        manthan-triage        (flash-lite)  Stripe webhook intake +
                                                       route_event A2A skill ->
                                                       calls the investigator
  investigator.py  manthan-investigator  (pro)         investigate_dispute A2A
                                                       skill; runs the ADK agent
                                                       IN-PROCESS and writes its
                                                       own events/projections via
                                                       services.case_store
  advisor.py       manthan-advisor       (flash)       conversational face:
                                                       ask / precheck_refund /
                                                       get_customer_history /
                                                       dispute_exposure /
                                                       contribute_evidence + the
                                                       6 read skills

The old NOTIFY-mirror investigate worker is gone; there is no pipeline
between the webhook and the brief - just agents calling agents over A2A
and the investigator writing the same events/cases/findings/actions rows
the merchant UI already reads.
"""
