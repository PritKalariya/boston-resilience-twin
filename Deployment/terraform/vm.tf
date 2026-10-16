# Airflow in Docker (ADR-010). Stopped when idle in October, on 24/7 from Nov 1.
# Running cost is about $31 a month (VM, disk, and ephemeral IP).
resource "google_compute_instance" "airflow" {
  project      = var.project_id
  name         = "airflow-vm"
  zone         = var.zone
  machine_type = "e2-medium"

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 30
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"

    # Ephemeral external IP: no nat_ip, so no static address is reserved and
    # the IP is released while the VM is stopped.
    access_config {}
  }

  service_account {
    email  = google_service_account.airflow_vm.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  # Attributes that move on their own, so a stopped VM still plans clean:
  # the ephemeral IP is released on stop and reassigned on start, the image
  # family resolves to a newer image over time, and OS Login manages SSH keys.
  lifecycle {
    ignore_changes = [
      boot_disk[0].initialize_params[0].image,
      network_interface[0].access_config[0].nat_ip,
      metadata["ssh-keys"],
    ]
  }

  depends_on = [google_project_service.project_1]
}
