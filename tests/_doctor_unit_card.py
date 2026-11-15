"""A real "Alterar dados" services-at-unit card (TASK-083) for the Portal reader tests.

The clinic: Dr. Beto attends at Centro and Zona Sul and offers "Consulta" (done ONLY at
Zona Sul) plus `AT_UNIT` (no marks: done at every unit). The card is built by the edit
flow's own `_service_list(at_unit=Centro)` over the worker-shaped snapshots (units and
marks attached like the worker does), then sent through the real Portal sender. Its
stored scope is `doctor_unit`, which the readers (options route, tap check) must resolve
with the doctors' units, not from the bare simulation snapshots.
"""

from uuid import uuid4

from secretaria.models import Professional, ProfessionalUnit, Tenant, Unit
from secretaria.models.professional_service_unit import ProfessionalServiceUnit
from secretaria.services import appointment_edit_flow as aef
from secretaria.services.channel_sender import RECORDED_MESSAGE_ID, BrainMessageSender
from secretaria.services.tenant_config import list_active_professionals
from secretaria.services.unit_booking import attach_unit_options
from secretaria.workers.shared.bubbles import _send_bubble
from secretaria.workers.shared.flow_simulation_input import load_simulation_inputs
from tests._edit_flow_support import appointment, draft_for

WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
HOURS = {day: [{"start": "08:00", "end": "18:00"}] for day in WEEK}
CONSULTA_ID = uuid4()
# 12 services done at Centro: one more than a Portal page (10) holds.
AT_UNIT = tuple(f"Serviço {i:02d}" for i in range(12))


async def seed_doctor_units(db, tenant_id):
    """Give the clinic Dr. Beto, two units and the Consulta-only-at-Sul mark."""
    async with db() as session, session.begin():
        tenant = await session.get(Tenant, tenant_id)
        tenant.business_hours = HOURS
        centro = Unit(id=uuid4(), tenant_id=tenant_id, name="Centro", address="Rua A, 10")
        sul = Unit(id=uuid4(), tenant_id=tenant_id, name="Zona Sul", address="Av. B, 20")
        beto = Professional(
            id=uuid4(),
            tenant_id=tenant_id,
            name="Dr. Beto",
            is_active=True,
            appointment_types=[
                {
                    "name": "Consulta",
                    "duration_min": 30,
                    "is_active": True,
                    "service_id": str(CONSULTA_ID),
                }
            ]
            + [{"name": n, "duration_min": 30, "is_active": True} for n in AT_UNIT],
        )
        session.add_all([centro, sul, beto])
        await session.flush()
        session.add_all(
            [
                ProfessionalUnit(professional_id=beto.id, unit_id=centro.id, sort_order=0),
                ProfessionalUnit(professional_id=beto.id, unit_id=sul.id, sort_order=1),
                ProfessionalServiceUnit(
                    professional_id=beto.id, service_id=CONSULTA_ID, unit_id=sul.id
                ),
            ]
        )
    return beto, centro, sul


async def send_doctor_unit_card(db, tenant_id, conversation, beto, centro, *, to="x") -> str:
    """The edit flow's services-at-Centro card for Dr. Beto, sent to the Portal; its id."""
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        inputs = await load_simulation_inputs(session, tenant)
        rows = await list_active_professionals(session, tenant.id)
        await attach_unit_options(session, tenant, rows, inputs.professionals)
    snapshot = next(p for p in inputs.professionals if p.id == beto.id)
    at_unit = next(u for u in snapshot.units if u.id == centro.id)
    draft = draft_for(appointment(professional_id=str(beto.id))).with_stage(
        pending_doctor=str(beto.id)
    )
    result = aef._service_list(draft, inputs.tenant, inputs.professionals, None, at_unit=at_unit)
    bubble = result.bubbles[-1]
    assert bubble.source.scope["list"] == "doctor_unit"
    response = await _send_bubble(
        BrainMessageSender(conversation_id=conversation.id, session_factory=db), to, bubble
    )
    return response[RECORDED_MESSAGE_ID]
