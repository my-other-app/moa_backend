import uuid
from datetime import datetime
from fastapi.encoders import jsonable_encoder
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import Field
from app.config import settings
from app.api.payments.schemas import (
    OrderCreateRequest,
    OrderCreateResponse,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
)
from app.core.auth.dependencies import AdminAuth, DependsAuth
from app.core.auth.permissions import MoneyAuth
from app.db.core import SessionDep
from app.api.payments.models import PaymentStatus
from app.core.response.base_model import CustomBaseModel
from app.core.response.pagination import (
    PaginatedResponse,
    PaginationParams,
    paginated_response,
)
from app.api.payments import service
from app.api.audit import service as audit
from app.response import CustomHTTPException


router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/orders", summary="Create a new payment order")
async def create_order(
    request: OrderCreateRequest, session: SessionDep, user: DependsAuth
) -> OrderCreateResponse:
    try:
        order = await service.create_razorpay_order(
            session=session,
            source=request.source,
            payload=request.payload,
            user_id=user.id,
        )

        return jsonable_encoder(order)

    except Exception as e:
        await session.rollback()
        raise CustomHTTPException(
            status_code=500, message=f"Failed to create order: {str(e)}"
        )


@router.post("/verify", summary="Verify a payment")
async def verify_payment(
    request: PaymentVerifyRequest,
    session: SessionDep,
    background_tasks: BackgroundTasks,
) -> PaymentVerifyResponse:

    payment = await service.verify_razorpay_payment(
        session=session,
        razorpay_order_id=request.razorpay_order_id,
        razorpay_payment_id=request.razorpay_payment_id,
        background_tasks=background_tasks,
    )

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    return {
        "payment_status": payment.status.value,
        # payment.amount / order.amount are already rupees (Numeric columns).
        "payment_amount": float(payment.amount),
        "remining_amount": float(payment.order.amount - payment.amount),
        "payment_method": payment.payment_method,
    }


@router.post("/logs/{payment_log_id}/refund", summary="Refund a captured payment (admin, full refund only)")
async def refund_payment(
    payment_log_id: uuid.UUID,
    session: SessionDep,
    # Money leaving the platform needs the money capability, not just admin.
    admin: MoneyAuth,
    request: Request = None,
) -> dict:
    payment = await service.refund_payment(session, payment_log_id)
    # Money leaving the platform is the action most worth being able to
    # attribute later.
    await audit.record(
        session,
        actor=admin,
        action="payment.refund",
        entity_type="payment",
        entity_id=payment_log_id,
        summary=f"Refunded {payment.amount}",
        details={
            "amount": float(payment.amount or 0),
            "razorpay_payment_id": payment.razorpay_payment_id,
        },
        request=request,
    )
    await session.commit()
    return {
        "payment_status": payment.status.value,
        "order_status": payment.order.status.value,
    }


@router.post("/webhook")
async def razorpay_webhook(request: Request, session: SessionDep):
    webhook_signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id", "")

    request_body = await request.body()
    request_body = request_body.decode("utf-8")
    payload = await request.json()

    try:
        if not service.razorpay_client.utility.verify_webhook_signature(
            request_body, webhook_signature, settings.RAZORPAY_WEBHOOK_SECRET
        ):
            raise Exception("Invalid webhook signature")
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid webhook signature: {str(e)}"
        )

    return await service.handle_razorpay_webhook(
        session, event_id=event_id, data=payload, signature=webhook_signature
    )


# @router.get("/orders/{order_id}", response_model=OrderResponse)
# async def get_order(order_id: uuid.UUID, db: Session = Depends(get_db)):
#     order = db.query(PaymentOrders).filter(PaymentOrders.id == order_id).first()
#     if not order:
#         raise HTTPException(status_code=404, detail="Order not found")

#     return OrderResponse(
#         id=order.id,
#         razorpay_order_id=order.razorpay_order_id,
#         amount=order.amount,
#         currency=order.currency,
#         status=order.status.value,
#         key_id=settings.RAZORPAY_KEY_ID,
#     )

# Check payment status
# @router.get("/status/{razorpay_order_id}")
# async def check_payment_status(razorpay_order_id: str, db: Session = Depends(get_db)):
#     # Get order from database
#     order = (
#         db.query(PaymentOrders)
#         .filter(PaymentOrders.razorpay_order_id == razorpay_order_id)
#         .first()
#     )

#     if not order:
#         raise HTTPException(status_code=404, detail="Order not found")

#     # Get payment logs
#     payment_logs = db.query(PaymentLogs).filter(PaymentLogs.order_id == order.id).all()

#     # Get order status from Razorpay
#     try:
#         razorpay_order = razorpay_client.order.fetch(razorpay_order_id)

#         # Update order status if needed
#         if razorpay_order.get("status") == "paid" and order.status != OrderStatus.paid:
#             order.status = OrderStatus.paid
#             db.commit()

#         return {
#             "order_id": order.id,
#             "razorpay_order_id": order.razorpay_order_id,
#             "amount": order.amount,
#             "currency": order.currency,
#             "status": order.status.value,
#             "razorpay_status": razorpay_order.get("status"),
#             "payments": [
#                 {
#                     "id": log.id,
#                     "razorpay_payment_id": log.razorpay_payment_id,
#                     "status": log.status.value,
#                     "amount_paid": log.amount_paid,
#                     "payment_method": log.payment_method,
#                     "created_at": log.created_at,
#                 }
#                 for log in payment_logs
#             ],
#         }

#     except Exception as e:
#         # If we can't reach Razorpay, just return our DB status
#         return {
#             "order_id": order.id,
#             "razorpay_order_id": order.razorpay_order_id,
#             "amount": order.amount,
#             "currency": order.currency,
#             "status": order.status.value,
#             "razorpay_status": "unknown",
#             "error": str(e),
#             "payments": [
#                 {
#                     "id": log.id,
#                     "razorpay_payment_id": log.razorpay_payment_id,
#                     "status": log.status.value,
#                     "amount_paid": log.amount_paid,
#                     "payment_method": log.payment_method,
#                     "created_at": log.created_at,
#                 }
#                 for log in payment_logs
#             ],
#         }


class AdminPaymentUser(CustomBaseModel):
    id: int | None = Field(None)
    full_name: str | None = Field(None)
    email: str | None = Field(None)


class AdminPaymentResponse(CustomBaseModel):
    id: uuid.UUID = Field(...)
    razorpay_payment_id: str | None = Field(None)
    status: str = Field(...)
    amount: float = Field(0)
    payment_method: str | None = Field(None)
    created_at: datetime = Field(...)
    order_id: uuid.UUID | None = Field(None)
    razorpay_order_id: str | None = Field(None)
    receipt: str | None = Field(None)
    order_status: str | None = Field(None)
    user: AdminPaymentUser | None = Field(None)
    # Only a captured payment can be refunded; surfacing it here saves the UI
    # re-deriving the rule and getting it wrong.
    is_refundable: bool = Field(False)


@router.get("/admin/list", summary="List payments (admin)")
async def list_payments_admin(
    request: Request,
    session: SessionDep,
    pagination: PaginationParams,
    admin: AdminAuth,
    status: str | None = Query(None, description="captured, refunded, failed, ..."),
    search: str | None = Query(None, description="Payment, order or receipt id"),
    user_id: int | None = Query(None),
) -> PaginatedResponse[AdminPaymentResponse]:
    """
    Every payment. There was no list endpoint at all, so the refund route
    required an id an admin had no way to find.
    """
    rows, total = await service.list_payments(
        session,
        status=status,
        search=search,
        user_id=user_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    items = [
        AdminPaymentResponse(
            id=row.id,
            razorpay_payment_id=row.razorpay_payment_id,
            status=row.status.value if hasattr(row.status, "value") else str(row.status),
            amount=float(row.amount or 0),
            payment_method=row.payment_method,
            created_at=row.created_at,
            order_id=row.order_id,
            razorpay_order_id=row.order.razorpay_order_id if row.order else None,
            receipt=row.order.receipt if row.order else None,
            order_status=(
                row.order.status.value
                if row.order and hasattr(row.order.status, "value")
                else None
            ),
            user=(
                AdminPaymentUser(
                    id=row.order.user.id,
                    full_name=row.order.user.full_name,
                    email=row.order.user.email,
                )
                if row.order and row.order.user
                else None
            ),
            is_refundable=row.status == PaymentStatus.captured,
        )
        for row in rows
    ]
    return paginated_response(
        items, request, schema=AdminPaymentResponse, total=total
    )


@router.get("/admin/totals", summary="Payment totals (admin)")
async def payment_totals_admin(session: SessionDep, admin: AdminAuth) -> dict:
    return await service.get_payment_totals(session)
