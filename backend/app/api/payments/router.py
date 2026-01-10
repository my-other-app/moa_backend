from fastapi.encoders import jsonable_encoder
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from app.config import settings
from app.api.payments.schemas import (
    OrderCreateRequest,
    OrderCreateResponse,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
)
from app.core.auth.dependencies import DependsAuth
from app.db.core import SessionDep
from app.api.payments import service
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
        "payment_amount": payment.amount / 100,
        "remining_amount": payment.order.amount - (payment.amount / 100),
        "payment_method": payment.payment_method,
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
