"""Client-supplied legal text (received 2026-08-27, from their current
intake form). Rendered as must-open accordions at booking and stored as
versioned waiver documents. Edit ONLY with client sign-off — this is their
legal wording, not marketing copy."""

WAIVER_VERSION = 2

WAIVER_RELEASE = """I understand and have read and completed this questionnaire truthfully and agree that this constitutes full disclosure, and supersedes any previous verbal or written disclosures. I understand that withholding information or providing misinformation may result in contraindications and/or irritation from any classes received. I am aware that it is my responsibility to inform Box2fit of my current medical or health conditions and to update this in the future if changes occur to my medical or physical condition(s).

The classes I receive at Box2fit are voluntary and I release this institution and Frankie LaSasso from any liability and assume full responsibility thereof. I agree Frankie LaSasso, and all Box2fit employees and Trainers are not responsible for any damages that result from any accident or lost and/or stolen items.

By signing this document, you and your family are giving up the right to sue in consideration for the Box2fit Club who is permitting the use of their equipment, programs and facility.

I agree to waive any claims that I have or may have in the future against Frankie LaSasso, Box2fit, and all employees thereof."""

ELECTRONIC_SIGNATURES = """Each party agrees that this Agreement and any other documents to be delivered in connection herewith may be electronically signed, and that any electronic signatures appearing on this Agreement or such other documents are the same as handwritten signatures for the purposes of validity, enforceability, and admissibility."""

MEMBERSHIP_TERMS = """All participants are entitled to a free trial class with no obligation to sign up or purchase a membership. Signing this waiver for a free trial does not create any payment obligation. The following terms and conditions apply only if the participant chooses to voluntarily sign up for a membership or paid service with Box2Fit.

1. Memberships are billed on a month-to-month basis and are subject to a mandatory 30-day cancellation notice after the initial 30 days of membership.

2. Cancellation requests must be submitted in writing by email or through the company's approved cancellation process. Verbal cancellations will not be accepted.

3. The member understands and agrees that membership dues will continue to be charged during the required 30-day notice period.

4. Failure to provide proper cancellation notice does not relieve the member of their financial obligation under this agreement.

5. Any outstanding balances, declined payments, chargebacks, or unpaid membership dues may be referred to a third-party collections agency. The member agrees to be responsible for all outstanding balances, including any applicable administrative, collection, or legal fees permitted by law.

6. By voluntarily signing up for a membership or paid service, the member confirms that they have read, understood, and agreed to the terms of this cancellation and payment policy."""

WAIVER_SECTIONS = [
    ("Waiver & Release of Liability (legal document)", WAIVER_RELEASE),
    ("Electronic Signatures", ELECTRONIC_SIGNATURES),
    (
        "Membership Terms, Cancellation Policy & Payment Authorization",
        MEMBERSHIP_TERMS,
    ),
]

WAIVER_FULL_TEXT = "\n\n---\n\n".join(
    f"## {title}\n\n{body}" for title, body in WAIVER_SECTIONS
)

# The one-line summary of the cancellation policy used wherever marketing
# copy needs to reference it (replaces the old "no contracts" claims).
CANCELLATION_SUMMARY = (
    "Memberships are month to month with a 30-day cancellation notice "
    "(free trial carries no obligation)."
)
