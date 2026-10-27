"""Regression checks for rights-scoped public market data."""
import pandas as pd
import pytest
from validation.prepare import _check_market

def test_rank_only_v3_roundtrip(tmp_path):
    folder=tmp_path
    (folder/'V3_summary.json').write_text('{"status":"COMPUTED","results":{"2025":{"positive_pair_country_technology_count":3,"spearman_positive_pairs":1.0}},"population_candidate_count":65}')
    pd.DataFrame({"country_key":["a","b","c"],"surface":["onshore"]*3,"gwpt_mw":[10,20,30],"gwpt_rank":[1.,2.,3.],"irena_rank":[1.,2.,3.]}).to_csv(folder/'V3_irena_gwpt_2025.csv',index=False)
    assert _check_market(folder)["years"] == ["2025"]
    table=pd.read_csv(folder/'V3_irena_gwpt_2025.csv'); table['irena_mw']=[1,2,3]
    table.to_csv(folder/'V3_irena_gwpt_2025.csv',index=False)
    with pytest.raises(ValueError,match='schema'):
        _check_market(folder)
