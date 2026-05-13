"""Tests for field type inference helpers (Step 2)."""
import datetime
import pytest
from vaults import FieldType, infer_field_type, infer_link_target


# --- infer_field_type ---

def test_boolean():
    assert infer_field_type([True, False, True]) == FieldType.BOOLEAN


def test_boolean_not_confused_by_int():
    # booleans are ints in Python; bool check must come first
    assert infer_field_type([True, False]) == FieldType.BOOLEAN
    assert infer_field_type([0, 1, 2]) == FieldType.NUMBER


def test_number_int():
    assert infer_field_type([1, 2, 3]) == FieldType.NUMBER


def test_number_float():
    assert infer_field_type([1.0, 2.5]) == FieldType.NUMBER


def test_date_string_stays_string():
    # Quoted date strings in YAML are strings — no pattern inference
    assert infer_field_type(["2021-06", "2023-12"]) == FieldType.STRING
    assert infer_field_type(["2008-05-07", "2021-01-01"]) == FieldType.STRING
    assert infer_field_type(["2026-03-01T12:59:00"]) == FieldType.STRING


def test_date_native_python_type():
    # Unquoted YAML dates → datetime.date → DATE
    assert infer_field_type([datetime.date(2021, 3, 1), datetime.date(2022, 7, 15)]) == FieldType.DATE


def test_datetime_native_python_type():
    # Unquoted YAML datetimes → datetime.datetime → DATETIME
    assert infer_field_type([datetime.datetime(2021, 3, 1, 9, 0, 0)]) == FieldType.DATETIME


def test_datetime_not_confused_with_date():
    # datetime.datetime is a subclass of datetime.date — must check datetime first
    assert infer_field_type([datetime.datetime(2021, 3, 1, 9, 0, 0)]) != FieldType.DATE


def test_string():
    assert infer_field_type(["Parlamentarischer Staatssekretär", "Bürgermeister"]) == FieldType.STRING


def test_link_scalar():
    assert infer_field_type(["[[E_A_Kollektive_Akteure/SPD|SPD]]"]) == FieldType.LINK


def test_list_links():
    values = [
        ["[[E_A_Kollektive_Akteure/RWTH Aachen|RWTH Aachen]]", "[[E_A_Kollektive_Akteure/Stadt Aachen|Stadt Aachen]]"],
        ["[[E_A_Kollektive_Akteure/SPD|SPD]]"],
    ]
    assert infer_field_type(values) == FieldType.LIST_LINKS


def test_list_strings():
    assert infer_field_type([["Zivilgesellschaft"], ["Wirtschaft", "Politik"]]) == FieldType.LIST_STRINGS


def test_list_mixed():
    values = [["[[E_A_Kollektive_Akteure/SPD|SPD]]", "plain string"]]
    assert infer_field_type(values) == FieldType.LIST_MIXED


def test_unknown_all_null():
    assert infer_field_type([None, "", []]) == FieldType.UNKNOWN


def test_unknown_empty():
    assert infer_field_type([]) == FieldType.UNKNOWN


def test_null_values_ignored():
    # nulls should not affect the inferred type
    assert infer_field_type([None, "", datetime.date(2021, 6, 1), datetime.date(2023, 12, 31)]) == FieldType.DATE


def test_empty_lists_ignored():
    assert infer_field_type([[], ["Zivilgesellschaft"]]) == FieldType.LIST_STRINGS


# --- infer_link_target ---

def test_link_target_scalar():
    values = ["[[E_A_Kollektive_Akteure/SPD|SPD]]", "[[E_A_Kollektive_Akteure/CDU|CDU]]"]
    assert infer_link_target(values) == "E_A_Kollektive_Akteure"


def test_link_target_list():
    values = [
        ["[[E_A_Kollektive_Akteure/RWTH Aachen|RWTH Aachen]]", "[[E_A_Kollektive_Akteure/Stadt Aachen|Stadt Aachen]]"],
        ["[[E_A_Kollektive_Akteure/SPD|SPD]]"],
    ]
    assert infer_link_target(values) == "E_A_Kollektive_Akteure"


def test_link_target_mixed_folders():
    values = ["[[E_A_Kollektive_Akteure/SPD|SPD]]", "[[E_A_Personen/Müller|Müller]]"]
    assert infer_link_target(values) is None


def test_link_target_no_links():
    assert infer_link_target(["plain", "strings"]) is None


def test_link_target_nulls_ignored():
    values = [None, "", "[[w_Städte/Aachen|Aachen]]"]
    assert infer_link_target(values) == "w_Städte"


def test_link_without_folder_prefix():
    # wikilinks without a folder prefix → no target folder extractable
    assert infer_link_target(["[[Aachen]]"]) is None
