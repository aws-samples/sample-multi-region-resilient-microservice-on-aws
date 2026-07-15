from pathlib import Path


ROOT = Path(__file__).parents[1]
GAMMA_SCRIPT = ROOT / "deployment" / "ngrh-gamma-create.sh"
NGRH_TEMPLATE = ROOT / "deployment" / "ngrh.yaml"
MAKEFILE = ROOT / "deployment" / "Makefile"


def test_gamma_script_resolves_both_regional_vpc_stacks():
    script = GAMMA_SCRIPT.read_text()

    assert 'VPC_P=$(resolve_stack "baseVpc${ENV}" "$PRIMARY_REGION")' in script
    assert 'VPC_S=$(resolve_stack "baseVpc${ENV}" "$STANDBY_REGION")' in script


def test_gamma_script_adds_vpc_stacks_to_every_service():
    script = GAMMA_SCRIPT.read_text()
    service_sections = ("ui", "catalog", "cart", "checkout", "orders", "assets")

    for index, service in enumerate(service_sections):
        start = script.index(f"# --- {service}:")
        if index + 1 < len(service_sections):
            end = script.index(f"# --- {service_sections[index + 1]}:", start)
        else:
            end = script.index("# Summary", start)
        section = script[start:end]

        assert '"$VPC_P"' in section, f"primary VPC missing from {service}"
        assert '"$VPC_S"' in section, f"standby VPC missing from {service}"


def test_vpc_sources_are_not_in_declarative_deployment_path():
    assert "BaseVpcArnPrimary" not in NGRH_TEMPLATE.read_text()
    assert "BaseVpcArnStandby" not in NGRH_TEMPLATE.read_text()
    assert "BaseVpcArnPrimary" not in MAKEFILE.read_text()
    assert "BaseVpcArnStandby" not in MAKEFILE.read_text()


def test_gamma_script_checks_existing_sources_using_api_response_shape():
    script = GAMMA_SCRIPT.read_text()

    assert ".inputSourceSummaries[]? | select(.cfnStackArn == $s)" in script
    assert ".resourceConfiguration.cfnStackArn" not in script
