"""EHR provider integrations (the `ehr` addon's push targets).

`base.py` defines the `EhrProvider` protocol every provider implements;
`plugins/ehr.py` selects a provider per tenant (`Tenant.ehr_provider`) from
the `PROVIDERS` registry it owns. Only `iclinic.py` exists today, and even
that is a STUB — see its module docstring for the real-integration TODO and
the list of future providers (Doctoralia, Memed, Conexa).
"""
