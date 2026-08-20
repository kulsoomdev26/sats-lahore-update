from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Email, Optional, EqualTo

from app.models.user import UserRole
from app.models.shift import ShiftName
from app.models.aircraft import AircraftCategory


class UserForm(FlaskForm):
    employee_id = StringField("Employee ID", validators=[DataRequired(), Length(max=30)])
    full_name = StringField("Full Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    designation = StringField("Designation", validators=[Optional(), Length(max=100)])
    role = SelectField("Role", choices=[(r.value, r.label) for r in UserRole], validators=[DataRequired()])
    station_id = SelectField("Station", coerce=int, validators=[Optional()])
    password = PasswordField(
        "Password",
        validators=[Optional(), Length(min=8, message="Password must be at least 8 characters.")],
    )
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save User")


class ResetPasswordForm(FlaskForm):
    new_password = PasswordField(
        "New Password",
        validators=[DataRequired(), Length(min=8, message="Password must be at least 8 characters.")],
    )
    confirm_password = PasswordField(
        "Confirm New Password",
        validators=[DataRequired(), EqualTo("new_password", message="Passwords must match.")],
    )
    submit = SubmitField("Reset Password")


class StationForm(FlaskForm):
    code = StringField("Station Code", validators=[DataRequired(), Length(max=10)])
    name = StringField("Station Name", validators=[DataRequired(), Length(max=150)])
    city = StringField("City", validators=[Optional(), Length(max=100)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Station")


class ShiftForm(FlaskForm):
    name = SelectField("Shift Name", choices=[(s.value, s.label) for s in ShiftName], validators=[DataRequired()])
    station_id = SelectField("Station", coerce=int, validators=[DataRequired()])
    shift_incharge_id = SelectField("Shift Incharge", coerce=int, validators=[Optional()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Shift")


class AircraftForm(FlaskForm):
    registration = StringField("Registration", validators=[DataRequired(), Length(max=20)])
    aircraft_type = StringField("Aircraft Type", validators=[DataRequired(), Length(max=100)])
    airline_id = SelectField("Airline", coerce=int, validators=[DataRequired()])
    category = SelectField("Category", choices=[(c.value, c.label) for c in AircraftCategory], validators=[DataRequired()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Aircraft")


class AirlineForm(FlaskForm):
    name = StringField("Airline Name", validators=[DataRequired(), Length(max=150)])
    iata_code = StringField("IATA Code", validators=[Optional(), Length(max=5)])
    icao_code = StringField("ICAO Code", validators=[Optional(), Length(max=5)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Airline")


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=150)])
    description = StringField("Description", validators=[Optional(), Length(max=300)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save")
