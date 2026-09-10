"""Hypervisor adapters. Only VMware Workstation is implemented in this version."""

from ntdrive.hypervisor.base import HypervisorAdapter, SnapshotNode, SnapshotTree

__all__ = ["HypervisorAdapter", "SnapshotNode", "SnapshotTree"]
