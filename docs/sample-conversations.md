# Sample conversations

Six screenings from the demo data on the deployed app. The candidates are the simulator's personas from the eval cases (a model playing each candidate), who applied and chatted through the app's public API on Monday 26 October 2026, 10:39–10:44 in Spain and 03:39–03:44 in Mexico. Prompt version `f32df8d2`.

Each conversation shows what the recruiter sees (key data, summary, decision), then the transcript as stored. *Template* marks a message sent from a fixed template; everything else the agent wrote. The decisions were made by the demo script, logged in as a recruiter.

## 1. Qualified, in English

Tom applies in English for Barcelona Gràcia and qualifies.\
He names a landmark instead of a postal code ("near Sagrada Família"): it's saved as a place in the Gràcia area, with no follow-up question.

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Qualified**

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Tom Becker |
| ✓ | Driving license | yes (German EU driving licence, valid in Spain) |
| ✓ | Service area | lives at Sagrada Família, Barcelona, in the area of the Barcelona Gràcia restaurant, in Barcelona |
| ✓ | Availability | full_time |
| ✓ | Schedule | flexible |
| ✓ | Experience | 3 years: Deliveroo, Uber Eats |
| ✓ | Start date | 2026-10-26 (Monday) |

**Summary.** Check before deciding: – · Notes: Tiene experiencia de reparto en Berlín.

### Transcript

> **Assistant · *template* `first_message`:** Hi Tom 👋\
> I'm Sazi, Grupo Sazón's virtual assistant.\
> Can I ask you a few quick questions about your application to deliver in Barcelona? It takes about 3 minutes.

> **Candidate:** Hi Sazi, sure, go ahead!

> **Assistant:** The hiring team will see your answers.\
> Is your full name Tom Becker?

> **Candidate:** Yes, Tom Becker.

> **Assistant:** Tom, do you have a driving licence valid today for driving in Spain?

> **Candidate:** Yes, I have a German driving licence. It's an EU licence, so it's valid in Spain.

> **Assistant:** To find the restaurant closest to you: what’s your postal code, or the neighbourhood you live in?

> **Candidate:** I live near Sagrada Família in Barcelona.

> **Assistant:** What availability do you have: full-time, part-time, or weekends?

> **Candidate:** I'm available full-time.

> **Assistant:** Which schedule do you prefer: mornings, afternoons, evenings, or flexible?

> **Candidate:** I'm flexible, any time works for me.

> **Assistant:** How much delivery experience do you have, and which platforms or companies have you worked for?

> **Candidate:** I have three years of delivery experience with Deliveroo and Uber Eats in Berlin.

> **Assistant:** When could you start?

> **Candidate:** I can start right away.

> **Assistant:** Please check this is right:

> **Assistant:**
> - Tom Becker
> - Valid German EU driving licence for Spain
> - Sagrada Família, Barcelona
> - Full-time; flexible schedule
> - 3 years with Deliveroo and Uber Eats
> - Can start right away

> **Assistant:** Is everything correct?

> **Candidate:** Yes, that's all correct.

> **Assistant:** Your profile meets the requirements, Tom.

> **Assistant:** The hiring team will contact you about the next step. Good luck! 👋

## 2. A question answered from the job's facts, and a recruiter override

Marta (Madrid Chamberí) gives her license and postal code with her name, and asks if she needs her own vehicle: the answer comes from the client's FAQ, and the questions she already answered are skipped.\
The rules qualify her; the recruiter then decides not qualified, and she gets the decision template.

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Qualified** · decided **Not qualified** by Demo script: "Este restaurante cubrió sus puestos esta semana."

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Marta Ruiz Ortega |
| ✓ | Driving license | yes (carnet B en vigor desde hace 6 años) |
| ✓ | Service area | lives at 28015, Argüelles, in the area of the Madrid Chamberí restaurant, in Madrid |
| ✓ | Availability | part_time, weekends |
| ✓ | Schedule | afternoon, evening |
| ✓ | Experience | 1.5 years: Glovo, Just Eat |
| ✓ | Start date | 2026-11-02 (Monday) |

**Summary.** Check before deciding: – · Notes: –

### Transcript

> **Assistant · *template* `first_message`:** Hola, Marta 👋\
> Soy Sazi, el asistente virtual de Grupo Sazón.\
> ¿Te hago unas preguntas rápidas sobre tu solicitud para repartir en Madrid? Son unos 3 minutos.

> **Candidate:** sí, claro 🙂

> **Assistant:** El equipo de selección verá tus respuestas.\
> ¿Tu nombre completo es Marta Ruiz Ortega?

> **Candidate:** sí, soy Marta Ruiz Ortega. tengo el carnet B en vigor desde hace 6 años y vivo en Argüelles, 28015.

> **Assistant:** ¿Buscas jornada completa, media jornada o fines de semana?

> **Candidate:** busco media jornada y fines de semana, sobre todo por las tardes y noches. por cierto, ¿necesito tener moto o coche propio?

> **Assistant:** No necesitas vehículo propio: Grupo Sazón proporciona moto eléctrica, casco y mochila térmica.

> **Assistant:** ¿Cuánta experiencia tienes repartiendo y con qué plataformas o empresas?

> **Candidate:** tengo un año y medio de experiencia repartiendo con Glovo y Just Eat.

> **Assistant:** ¿Cuándo podrías empezar?

> **Candidate:** podría empezar el lunes 2 de noviembre 🙂

> **Assistant:** Resumen:
> - Marta Ruiz Ortega
> - Carnet B en vigor
> - Argüelles, 28015
> - Media jornada y fines de semana
> - Tardes y noches
> - 1 año y medio con Glovo y Just Eat
> - Empieza el 2 de noviembre

> **Assistant:** ¿Está todo correcto?

> **Candidate:** sí, todo correcto 🙂

> **Assistant:** Tu perfil cumple con los requisitos, Marta.

> **Assistant:** El equipo de selección te contactará pronto para el siguiente paso. ¡Mucha suerte! 👋

> **Assistant · *template* `decision_not_qualified`:** Hola, Marta. El equipo de selección ha revisado tu solicitud para repartir en Madrid y, por ahora, no podemos seguir adelante.\
> Gracias por tu tiempo. Si tienes alguna pregunta, escríbenos aquí.

## 3. A knockout: no driving license yet

Pablo (Valencia Ruzafa) has passed the theory test and takes the practical one in December: with no license today, he's not qualified, and the rest isn't asked.\
The agent says why, offers a person, and asks about other jobs (he says yes).

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Not qualified** (No valid driving license)

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Pablo Sanz Ferrer |
| ✓ | Driving license | no (Aprobó el teórico y se examina del práctico en diciembre) |
| – | Service area | not answered |
| – | Availability | not answered |
| – | Schedule | not answered |
| – | Experience | not answered |
| – | Start date | not answered |

**Summary.** Check before deciding: – · Notes: –

### Transcript

> **Assistant · *template* `first_message`:** Hola, Pablo 👋\
> Soy Sazi, el asistente virtual de Grupo Sazón.\
> ¿Te hago unas preguntas rápidas sobre tu solicitud para repartir en Valencia? Son unos 3 minutos.

> **Candidate:** Sí, claro 😊

> **Assistant:** El equipo de selección verá tus respuestas.\
> ¿Tu nombre completo es Pablo Sanz Ferrer?

> **Candidate:** Sí, soy Pablo Sanz Ferrer 😊

> **Assistant:** ¿Tienes carnet de conducir vigente para conducir en España?

> **Candidate:** Todavía no tengo carnet. Aprobé el teórico y me examino del práctico en diciembre.

> **Assistant:** Gracias por contármelo, Pablo. Este puesto requiere carnet de conducir en vigor, así que por ahora no podemos seguir.

> **Assistant:** Cuando lo tengas, vuelve a escribirnos. Si prefieres que lo revise una persona, dímelo.

> **Assistant:** ¿Te avisamos si hay otros puestos en Grupo Sazón?

> **Candidate:** Vale, lo entiendo. Sí, avisadme si sale algún otro puesto. ¡Gracias!

> **Assistant:** De acuerdo, Pablo. Te avisaremos si surge otro puesto que pueda encajarte.

> **Assistant:** ¡Gracias por tu tiempo!

## 4. Mexico: left for the team to review

Luis (Monterrey Centro) has a Colombian license and doesn't know if it's valid in Mexico: after two tries it stays unclear, and the screening goes to the team instead of becoming a no.\
The recruiter decides qualified, and he gets the decision template.

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Needs review** (Driving license unclear: licencia de Colombia; no sabe si es válida en México) · decided **Qualified** by Demo script: "Revisado por teléfono: puede seguir al siguiente paso."

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Luis Alberto Pérez Gómez |
| ⚑ | Driving license | unclear: licencia de Colombia; no sabe si es válida en México |
| ✓ | Service area | lives at 64060, Monterrey, in the area of the Monterrey Centro restaurant, in Monterrey |
| ✓ | Availability | full_time |
| ✓ | Schedule | morning |
| ✓ | Experience | 0 years |
| ✓ | Start date | 2026-10-26 (Monday) |

**Summary.** Check before deciding: – · Notes: –

### Transcript

> **Assistant · *template* `first_message`:** Hola, Luis 👋\
> Soy Sazi, el asistente virtual de Grupo Sazón.\
> ¿Te puedo hacer unas preguntas rápidas sobre tu solicitud para repartir en Monterrey? Son unos 3 minutos.

> **Candidate:** ¡Hola! Sí, claro, pregúntame 😊

> **Assistant:** El equipo de reclutamiento verá tus respuestas.\
> ¿Tu nombre completo es Luis Alberto Pérez Gómez?

> **Candidate:** Sí, mi nombre completo es Luis Alberto Pérez Gómez 😊

> **Assistant:** Luis, ¿tienes licencia de manejo vigente para manejar en México?

> **Candidate:** Tengo licencia de conducción de Colombia, pero no estoy seguro si sirve para manejar en México; nunca lo he averiguado.

> **Assistant:** ¿Sabes si tu licencia de Colombia es válida actualmente para manejar en México?

> **Candidate:** No estoy seguro si mi licencia de Colombia es válida para manejar en México; nunca lo he averiguado.

> **Assistant:** ¿En qué código postal, colonia o municipio vives?

> **Candidate:** Vivo en Monterrey, código postal 64060.

> **Assistant:** ¿Qué disponibilidad tienes: tiempo completo, medio tiempo o fines de semana?

> **Candidate:** Tengo disponibilidad de tiempo completo.

> **Assistant:** ¿Qué horario prefieres: mañana, tarde, noche o flexible?

> **Candidate:** Prefiero el horario de la mañana.

> **Assistant:** ¿Cuánta experiencia tienes haciendo entregas y con qué plataformas o empresas?

> **Candidate:** No tengo experiencia haciendo entregas ni he trabajado con plataformas o empresas de reparto.

> **Assistant:** ¿Cuándo podrías empezar?

> **Candidate:** Podría empezar de inmediato.

> **Assistant:** Resumen:
> - Nombre: Luis Alberto Pérez Gómez
> - Licencia: licencia de Colombia; validez en México sin confirmar
> - Zona: 64060, Monterrey
> - Disponibilidad: tiempo completo
> - Horario: mañana
> - Experiencia: ninguna
> - Inicio: de inmediato

> **Assistant:** ¿Está correcto?

> **Candidate:** Sí, todo está correcto 😊

> **Assistant:** Luis, una persona del equipo revisará tu solicitud y te escribirá por aquí.

> **Assistant · *template* `decision_qualified`:** Hola, Luis. El equipo de reclutamiento revisó tu solicitud para repartir en Monterrey y tu perfil encaja.\
> Te contactaremos pronto para el siguiente paso.

## 5. Mexico: a request for a person in the middle of the screening

Fernanda (CDMX Coyoacán) asks to talk to a person about the contract and benefits: the agent answers the contract part from the FAQ, tells her a person will write, and finishes the screening.\
Asking for a person is a review reason, so the screening goes to the team with what she wants to discuss.

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Needs review** (Asked for a person: consultar el tipo de contrato y las prestaciones)

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Fernanda Torres Medina |
| ✓ | Driving license | yes |
| ✓ | Service area | lives at 04360, Copilco, in the area of the CDMX Coyoacán restaurant, in Ciudad de México |
| ✓ | Availability | full_time |
| ✓ | Schedule | flexible |
| ✓ | Experience | 1 year: Uber Eats |
| ✓ | Start date | 2026-11-01 (Sunday) |

**Summary.** Check before deciding: – · Notes: –

### Transcript

> **Assistant · *template* `first_message`:** Hola, Fernanda 👋\
> Soy Sazi, el asistente virtual de Grupo Sazón.\
> ¿Te puedo hacer unas preguntas rápidas sobre tu solicitud para repartir en Ciudad de México? Son unos 3 minutos.

> **Candidate:** Hola, sí, claro. Dígame 😊

> **Assistant:** El equipo de reclutamiento verá tus respuestas.\
> ¿Tu nombre completo es Fernanda Torres Medina?

> **Candidate:** Sí, mi nombre completo es Fernanda Torres Medina.

> **Assistant:** ¿Tienes licencia de manejo vigente para manejar en México?

> **Candidate:** Sí, tengo licencia de manejo vigente para manejar en México.

> **Assistant:** Para ubicar el restaurante más cercano: ¿cuál es tu código postal o la colonia donde vives?

> **Candidate:** Vivo en Copilco, código postal 04360. ¿Podría hablar con una persona de reclutamiento? Me gustaría consultar el tipo de contrato y las prestaciones.

> **Assistant:** El puesto tiene contrato laboral con Grupo Sazón, seguridad social desde el primer día y un periodo de prueba de 2 meses.

> **Assistant:** Una persona del equipo te escribirá aquí para revisar las prestaciones. ¿Quieres seguir con las preguntas restantes?

> **Candidate:** Sí, claro, seguimos con las preguntas. Gracias por avisarme que alguien de reclutamiento me escribirá 😊

> **Assistant:** ¿Qué disponibilidad tienes: tiempo completo, medio tiempo o fines de semana?

> **Candidate:** Tengo disponibilidad de tiempo completo.

> **Assistant:** ¿Qué horario prefieres: mañana, tarde, noche o flexible?

> **Candidate:** Prefiero un horario flexible.

> **Assistant:** ¿Cuánta experiencia tienes haciendo entregas y con qué plataformas o empresas?

> **Candidate:** Tengo un año de experiencia haciendo entregas con Uber Eats.

> **Assistant:** ¿Cuándo podrías empezar?

> **Candidate:** Podría empezar el 1 de noviembre.

> **Assistant:** Resumen:
> - Nombre: Fernanda Torres Medina
> - Licencia vigente en México
> - Zona: Copilco, 04360
> - Tiempo completo; horario flexible
> - 1 año con Uber Eats
> - Inicio: 1 de noviembre

> **Assistant:** ¿Está correcto?

> **Candidate:** Sí, todo está correcto. Gracias 😊

> **Assistant:** Gracias, Fernanda. Una persona del equipo revisará tu solicitud y te escribirá aquí.

## 6. Mexico: answers given before they're asked

Itzel (CDMX Coyoacán) says she could start on Monday and has her own motorbike while confirming her name: the start date is never asked, and "el lunes" is saved as Monday 2 November.\
The motorbike isn't a screening field, so the summary carries it as a note for the recruiter.

### What the recruiter sees

**Outcome.** Completed · rules' outcome: **Qualified**

**Key data.**

| | Field | Answer |
|---|---|---|
| ✓ | Full name | Itzel Hernández Ruiz |
| ✓ | Driving license | yes (licencia vigente de CDMX para carro y moto) |
| ✓ | Service area | lives at 04100, colonia Del Carmen, Coyoacán, in the area of the CDMX Coyoacán restaurant, in Ciudad de México |
| ✓ | Availability | part_time |
| ✓ | Schedule | morning, afternoon |
| ✓ | Experience | 3 years: DiDi Food |
| ✓ | Start date | 2026-11-02 (Monday) |

**Summary.** Check before deciding: – · Notes: Indicó que tiene moto propia.

### Transcript

> **Assistant · *template* `first_message`:** Hola, Itzel 👋\
> Soy Sazi, el asistente virtual de Grupo Sazón.\
> ¿Te puedo hacer unas preguntas rápidas sobre tu solicitud para repartir en Ciudad de México? Son unos 3 minutos.

> **Candidate:** Sí, claro 😊

> **Assistant:** El equipo de reclutamiento verá tus respuestas.\
> ¿Tu nombre completo es Itzel Hernández Ruiz?

> **Candidate:** Sí, soy Itzel Hernández Ruiz 😊 Podría empezar el lunes y tengo moto propia.

> **Assistant:** ¿Tienes licencia de manejo vigente para manejar en México?

> **Candidate:** Sí, tengo licencia vigente de CDMX para carro y moto.

> **Assistant:** Para ubicar el restaurante más cercano: ¿cuál es tu código postal o la colonia donde vives?

> **Candidate:** Vivo en la colonia Del Carmen, Coyoacán, CP 04100.

> **Assistant:** ¿Qué disponibilidad tienes: tiempo completo, medio tiempo o fines de semana?

> **Candidate:** Medio tiempo 😊

> **Assistant:** ¿Prefieres trabajar por la mañana, tarde, noche o con horario flexible?

> **Candidate:** Me acomoda trabajar por la mañana o por la tarde 😊

> **Assistant:** ¿Cuánta experiencia tienes haciendo entregas y con qué plataformas o empresas has trabajado?

> **Candidate:** Tengo 3 años de experiencia haciendo entregas con DiDi Food 😊

> **Assistant:** Resumen:
> - Itzel Hernández Ruiz
> - Licencia vigente de CDMX para carro y moto
> - Colonia Del Carmen, Coyoacán, CP 04100
> - Medio tiempo; mañana o tarde
> - 3 años con DiDi Food
> - Inicio: lunes 2 de noviembre

> **Assistant:** ¿Está correcto?

> **Candidate:** Sí, todo correcto 😊

> **Assistant:** Tu perfil cumple con los requisitos, Itzel.

> **Assistant:** El equipo de reclutamiento te contactará por aquí para el siguiente paso. ¡Mucha suerte! 👋
