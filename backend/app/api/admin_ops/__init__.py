"""The admin portal's operator tools: club payouts, disputes, partial and
store refunds, admin accounts (invites, removal, required two-factor),
runtime platform settings, the job queue's failed and stuck jobs, the email
suppression list and the platform number's WhatsApp templates.

New code lives here rather than in the modules it reads (payments, ledger,
subscriptions, jobs, mail): each of those keeps its own job, and this package
is what an operator does with them.
"""
