"""Throwaway file to give the MR-review agent something to review. Safe to delete."""


def add(a, b):
    return a + b


def divide(a, b):
    # intentionally missing a zero-division guard — reviewable
    return a / b
