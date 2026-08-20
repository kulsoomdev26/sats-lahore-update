from flask_wtf import FlaskForm
from wtforms import TextAreaField, SubmitField
from wtforms.validators import DataRequired, Optional, Length


class ApproveForm(FlaskForm):
    """Approval remarks are optional - an approval needs no justification."""
    remarks = TextAreaField("Approval Remarks", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Approve")


class RejectForm(FlaskForm):
    """Rejection remarks are mandatory - the engineer must know exactly why
    their activity was rejected so they can correct and resubmit it."""
    remarks = TextAreaField(
        "Rejection Remarks",
        validators=[DataRequired(message="Rejection remarks are required."), Length(max=500)],
    )
    submit = SubmitField("Reject")
