#!/usr/bin/env python3
"""Grab Oracle Always Free instances in Tokyo. Free shapes only: A1.Flex <=2 OCPU/12 GB, E2.1.Micro.
Never creates anything paid. Skips if the target instance already exists."""
import os, sys, oci

MODE = os.environ.get("MODE", "a1")          # a1 | micro
TENANCY = os.environ["OCI_TENANCY"]
COMP = os.environ.get("OCI_COMPARTMENT") or TENANCY
SSH = os.environ["SSH_PUBKEY"]
import re
def _key(raw):
    raw = raw.replace("\\n", "\n").replace("\r", "").strip()
    m = re.search(r"-----BEGIN (RSA )?PRIVATE KEY-----.*?-----END (RSA )?PRIVATE KEY-----", raw, re.S)
    if not m:
        b64 = "".join(l.strip() for l in raw.splitlines() if re.fullmatch(r"[A-Za-z0-9+/=]+", l.strip() or "!"))
        if len(b64) > 1000:
            from cryptography.hazmat.primitives import serialization as S
            for h in ("PRIVATE KEY", "RSA PRIVATE KEY"):
                pem = f"-----BEGIN {h}-----\n" + "\n".join(b64[i:i+64] for i in range(0, len(b64), 64)) + f"\n-----END {h}-----\n"
                try: S.load_pem_private_key(pem.encode(), None); print(f"OCI_KEY had no BEGIN/END lines; rebuilt as {h}"); return pem
                except Exception: pass
        print(f"OCI_KEY looks wrong: len={len(raw)} lines={raw.count(chr(10))+1} "
              f"has_PRIVATE={'PRIVATE KEY' in raw} has_PUBLIC={'PUBLIC KEY' in raw} -> paste the *private* .pem")
        sys.exit(1)
    body = m.group(0)
    head, tail = body.split("-----", 2)[1], body.rsplit("-----", 2)[1]
    b64 = re.sub(r"-----[^-]+-----|\s", "", body)
    return f"-----{head}-----\n" + "\n".join(b64[i:i+64] for i in range(0, len(b64), 64)) + f"\n-----{tail}-----\n"
cfg = {"user": os.environ["OCI_USER"], "fingerprint": os.environ["OCI_FINGERPRINT"], "tenancy": TENANCY,
       "region": os.environ.get("OCI_REGION", "ap-tokyo-1"), "key_content": _key(os.environ["OCI_KEY"])}
oci.config.validate_config(cfg)
cc = oci.core.ComputeClient(cfg); vn = oci.core.VirtualNetworkClient(cfg); idc = oci.identity.IdentityClient(cfg)

if MODE.startswith("open"):  # open TCP ports in the public subnet's security lists, e.g. MODE=open:443
    ports = [int(p) for p in MODE.split(":")[1].split(",")]
    for sub in vn.list_subnets(COMP).data:
        if sub.prohibit_public_ip_on_vnic: continue
        for sl_id in sub.security_list_ids:
            sl = vn.get_security_list(sl_id).data
            rules = list(sl.ingress_security_rules); changed = False
            for p in ports:
                if any(r.protocol == "6" and r.source == "0.0.0.0/0" and r.tcp_options and r.tcp_options.destination_port_range
                       and r.tcp_options.destination_port_range.min <= p <= r.tcp_options.destination_port_range.max for r in rules):
                    print(f"{sl.display_name}: {p} already open"); continue
                rules.append(oci.core.models.IngressSecurityRule(protocol="6", source="0.0.0.0/0", is_stateless=False,
                    tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=p, max=p))))
                changed = True; print(f"{sl.display_name}: opening {p}")
            if changed:
                vn.update_security_list(sl_id, oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules))
    for i in oci.pagination.list_call_get_all_results(cc.list_instances, COMP).data:
        if i.lifecycle_state == "RUNNING": print(f"instance {i.display_name} {i.shape} RUNNING")
    sys.exit(0)

SPEC = {
  "a1":    dict(shape="VM.Standard.A1.Flex", ocpus=2, mem=12, boot=100, name="tokyo-arm"),
  "micro": dict(shape="VM.Standard.E2.1.Micro", ocpus=None, mem=None, boot=50, name="tokyo-micro"),
}[MODE]
LIVE = {"PROVISIONING", "STARTING", "RUNNING", "STOPPING", "STOPPED"}

def alive(shape):
    return [i for i in oci.pagination.list_call_get_all_results(cc.list_instances, COMP).data
            if i.shape == shape and i.lifecycle_state in LIVE]

# free-tier guard: A1 total must stay <=2 OCPU / 12 GB; micro <=2 instances
ex = alive(SPEC["shape"])
if MODE == "a1" and ex:
    print(f"A1 already exists: {[i.display_name for i in ex]} -> nothing to do"); sys.exit(0)
if MODE == "micro" and len(ex) >= 1:
    print(f"Micro already exists: {[i.display_name for i in ex]} -> nothing to do"); sys.exit(0)

# boot volume guard (Always Free 200 GB total)
used = 0
for ad in idc.list_availability_domains(TENANCY).data:
    for b in oci.pagination.list_call_get_all_results(oci.core.BlockstorageClient(cfg).list_boot_volumes, availability_domain=ad.name, compartment_id=COMP).data:
        if b.lifecycle_state not in ("TERMINATED", "TERMINATING"): used += b.size_in_gbs or 0
if used + SPEC["boot"] > 200:
    print(f"boot volumes would exceed 200 GB free ({used}+{SPEC['boot']}) -> abort"); sys.exit(1)

ad = idc.list_availability_domains(TENANCY).data[0].name
subs = [s for s in vn.list_subnets(COMP).data if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic]
if not subs: print("no public subnet found"); sys.exit(1)
subnet = subs[0].id
imgs = cc.list_images(COMP, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                      shape=SPEC["shape"], sort_by="TIMECREATED", sort_order="DESC").data
imgs = [i for i in imgs if "Minimal" not in i.display_name] or imgs
if not imgs: print("no Ubuntu 24.04 image for shape"); sys.exit(1)

d = oci.core.models.LaunchInstanceDetails(
    availability_domain=ad, compartment_id=COMP, display_name=SPEC["name"], shape=SPEC["shape"],
    metadata={"ssh_authorized_keys": SSH},
    create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnet, assign_public_ip=True),
    source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=imgs[0].id,
        boot_volume_size_in_gbs=SPEC["boot"], boot_volume_vpus_per_gb=10))
if SPEC["ocpus"]:
    d.shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=SPEC["ocpus"], memory_in_gbs=SPEC["mem"])
try:
    r = cc.launch_instance(d).data
    print(f"SUCCESS launched {r.display_name} {r.shape} state={r.lifecycle_state}")
    with open(os.environ.get("GITHUB_STEP_SUMMARY", "/dev/null"), "a") as f: f.write(f"## ✅ {r.display_name} launched\n")
except oci.exceptions.ServiceError as e:
    if e.status in (500, 429) or "capacity" in str(e.message).lower():
        print(f"no luck: {e.status} {e.message}"); sys.exit(0)   # normal: try again next run
    print(f"ERROR {e.status} {e.code} {e.message}"); sys.exit(1)
