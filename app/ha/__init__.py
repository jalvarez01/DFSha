"""
Alta disponibilidad del ControlNode (Hito 3): un líder y uno o más
respaldos (ControlNode <-> ControlNode).

- changelog.py: log de cambios de metadatos (captura y aplicación).
- state.py:     rol, época y seguimiento de respaldos y testigos.
- router.py:    API /ha/* entre ControlNodes.
- manager.py:   arranque, seguimiento del líder, failover y degradación.
- middleware.py: rechaza peticiones en los respaldos y hace esperar al
                 líder la confirmación del respaldo (replicación semi-síncrona).
"""
