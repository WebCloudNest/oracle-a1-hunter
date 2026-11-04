"""Try once to create the Always Free Oracle A1 server. Safe to run every few minutes.

Exit 0 + status=no_capacity  -> Oracle is full, try again next run
Exit 0 + status=created      -> server made (public IP in output)
Exit 0 + status=exists       -> server already there, nothing done
Exit 1                       -> real error (bad key, limit, etc.)
"""
import base64
import os
import sys

import oci

NAME = os.environ.get("INSTANCE_NAME", "claude-box")
SHAPE = "VM.Standard.A1.Flex"
OCPUS = float(os.environ.get("OCPUS", "2"))
MEMORY_GB = float(os.environ.get("MEMORY_GB", "12"))
BOOT_GB = int(os.environ.get("BOOT_GB", "50"))

# Free-tier guardrail: this tenancy's A1 limit is 2 OCPU / 12 GB.
if not (1 <= OCPUS <= 2 and 1 <= MEMORY_GB <= 12 and 47 <= BOOT_GB <= 200):
    sys.exit("Refusing: settings are outside the Always Free limits.")


def output(key, value):
    print(f"{key}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


config = {
    "user": os.environ["OCI_USER_OCID"],
    "tenancy": os.environ["OCI_TENANCY_OCID"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region": os.environ["OCI_REGION"],
    "key_content": os.environ["OCI_PRIVATE_KEY"],
}
oci.config.validate_config(config)
compartment = config["tenancy"]
compute = oci.core.ComputeClient(config)
network = oci.core.VirtualNetworkClient(config)
identity = oci.identity.IdentityClient(config)


def public_ip(instance_id):
    attachments = compute.list_vnic_attachments(compartment, instance_id=instance_id).data
    for a in attachments:
        if a.lifecycle_state == "ATTACHED":
            return network.get_vnic(a.vnic_id).data.public_ip or "pending"
    return "pending"


# 1. Already have one? Then stop - never create a second.
instances = oci.pagination.list_call_get_all_results(
    compute.list_instances, compartment, display_name=NAME
).data
alive = [i for i in instances if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if alive:
    output("status", "exists")
    output("public_ip", public_ip(alive[0].id))
    sys.exit(0)

# 2. Latest Ubuntu 24.04 Arm image
images = compute.list_images(
    compartment,
    operating_system="Canonical Ubuntu",
    operating_system_version="24.04",
    shape=SHAPE,
    sort_by="TIMECREATED",
    sort_order="DESC",
).data
image = next(i for i in images if "aarch64" in i.display_name and "Minimal" not in i.display_name)
ad = identity.list_availability_domains(compartment).data[0].name

with open(os.path.join(os.path.dirname(__file__), "cloud-init.yaml"), "rb") as f:
    user_data = base64.b64encode(f.read()).decode()

details = oci.core.models.LaunchInstanceDetails(
    availability_domain=ad,
    compartment_id=compartment,
    display_name=NAME,
    shape=SHAPE,
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=image.id, boot_volume_size_in_gbs=BOOT_GB
    ),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=os.environ["OCI_SUBNET_OCID"], assign_public_ip=True, hostname_label=NAME
    ),
    metadata={"ssh_authorized_keys": os.environ["SSH_PUBLIC_KEY"].strip(), "user_data": user_data},
)

# 3. Try to launch
try:
    instance = compute.launch_instance(details).data
except oci.exceptions.ServiceError as e:
    if "capacity" in (e.message or "").lower() or e.status in (429, 500, 503):
        print(f"No capacity yet ({e.status}): {e.message}")
        output("status", "no_capacity")
        sys.exit(0)
    raise

# 4. Wait until running, then report the IP
oci.wait_until(compute, compute.get_instance(instance.id), "lifecycle_state", "RUNNING", max_wait_seconds=900)
output("status", "created")
output("public_ip", public_ip(instance.id))
