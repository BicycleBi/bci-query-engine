import pytest

from app.composition import EmailComposition, compose, load_email_composition
from app.renderer import render


def test_ordered_slots_and_mapped_parameters():
    header = {'view_name':'public.header','template_body':'<h1>{{ rows[0].label }}</h1>'}
    values = {'view_name':'public.values','template_body':'<p>{{ rows[0].label }}</p>'}
    definition = EmailComposition('Period {{ parameters.month }}', [
        ('header', header, {'period':'month'}), ('values', values, None),
    ])
    calls = []

    def rows(view, query):
        calls.append((view, query))
        return [{'label':'<safe>'}]

    subject, html, count = compose(definition, "{{ slots['header'] }}{{ slots['values'] }}",
                                   {'month':'Oct','scope':'fixture'}, rows, render)
    assert calls == [('public.header',{'period':'Oct'}),
                     ('public.values',{'month':'Oct','scope':'fixture'})]
    assert subject == 'Period Oct'
    assert html == '<h1>&lt;safe&gt;</h1><p>&lt;safe&gt;</p>'
    assert count == 2


def test_invalid_send_subject_and_missing_slot_parameter():
    content = {'view_name':'public.report','template_body':'fixture'}
    definition = EmailComposition('{{ parameters.subject }}', [('body',content,{'scope':'scope'})])
    with pytest.raises(ValueError, match='required'):
        compose(definition, '{{ slots.body }}', {}, lambda *args: [], render)
    with pytest.raises(ValueError, match='subject'):
        compose(definition, '{{ slots.body }}', {'scope':'a','subject':'line1\nline2'},
                lambda *args: [], render)


def test_composition_rejects_cross_client_content():
    class Result:
        def __init__(self, rows):
            self.rows = rows
        def fetchone(self):
            return self.rows[0]
        def fetchall(self):
            return self.rows

    class Metadata:
        def execute(self, statement, parameters=None):
            if 'to_regclass' in statement:
                return Result([('app.artifact_email_composition',)])
            if 'subject_template' in statement:
                return Result([('subject',)])
            return Result([('body','child',None)])

    parent = {'artifact_id':'parent','client_key':'one','delivery_mode':'email','template_body':'layout'}
    child = {'client_key':'two','view_name':'public.content','template_body':'body'}
    with pytest.raises(ValueError, match='same-client'):
        load_email_composition(Metadata(), parent, lambda *a, **kw: child)
